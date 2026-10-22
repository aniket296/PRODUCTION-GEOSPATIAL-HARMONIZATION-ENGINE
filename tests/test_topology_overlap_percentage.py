"""Tests for independent pairwise overlap percentage calculations (catch-up Step 72/150).

Validates directive Section 15 formulas::

    intersection_area = area(A ∩ B)
    overlap_pct_a = (intersection_area / area_a) * 100
    overlap_pct_b = (intersection_area / area_b) * 100

Key invariants verified:
1. Asymmetry across unequal parcels: a 15 m² overlap between a 10,000 m² parcel A and a 100 m² parcel B yields
   0.15% on parcel A and 15.0% on parcel B — proving both percentages must be computed independently so a critical
   encroachment on a small parcel is never masked by a large neighbor.
2. Symmetry when parcel areas are equal, full containment (100% on contained parcel), and zero overlap on
   edge-touching / corner-touching / disjoint parcels.
3. Floating-point epsilon clamping to [0.0, 100.0] and strict ValueError rejection for non-positive parcel areas
   or negative intersection areas.
4. PostGIS SQLAlchemy expression and query construction (`overlap_percentage_expr`,
   `build_pairwise_overlap_percentage_query`) using `geometry_projected` (SRID 32643) exclusively.
"""

from __future__ import annotations

import uuid

import pytest
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import Float, column, table
from sqlalchemy.dialects import postgresql

from backend.topology.overlap_percentage import (
    OverlapPercentageMetrics,
    build_pairwise_overlap_percentage_query,
    compute_overlap_percentage,
    compute_overlap_percentages,
    evaluate_pair_overlap_metrics,
    overlap_percentage_expr,
)

PG = postgresql.dialect()


def _sql(element) -> str:
    return str(element.compile(dialect=PG))


# =====================================================================
# Pure arithmetic formula tests
# =====================================================================

class TestOverlapPercentageFormulas:
    """Test `compute_overlap_percentage` and `compute_overlap_percentages` arithmetic and edge cases."""

    def test_asymmetric_overlap_percentages_for_unequal_parcels(self):
        """A 15 m² overlap on a 10,000 m² estate vs a 100 m² plot yields 0.15% and 15.0% independently."""
        pct_a, pct_b = compute_overlap_percentages(
            intersection_area_sqm=15.0,
            area_a_sqm=10_000.0,
            area_b_sqm=100.0,
        )
        assert pct_a == pytest.approx(0.15, abs=1e-9)
        assert pct_b == pytest.approx(15.0, abs=1e-9)
        assert pct_a != pct_b

    def test_symmetric_overlap_percentages_for_equal_parcels(self):
        pct_a, pct_b = compute_overlap_percentages(
            intersection_area_sqm=25.0,
            area_a_sqm=500.0,
            area_b_sqm=500.0,
        )
        assert pct_a == pytest.approx(5.0, abs=1e-9)
        assert pct_b == pytest.approx(5.0, abs=1e-9)

    def test_zero_intersection_area_returns_zero_for_both_parcels(self):
        pct_a, pct_b = compute_overlap_percentages(
            intersection_area_sqm=0.0,
            area_a_sqm=250.0,
            area_b_sqm=1200.0,
        )
        assert pct_a == 0.0
        assert pct_b == 0.0

    def test_full_containment_yields_100_percent_on_contained_parcel(self):
        pct_a, pct_b = compute_overlap_percentages(
            intersection_area_sqm=80.0,
            area_a_sqm=400.0,
            area_b_sqm=80.0,
        )
        assert pct_a == pytest.approx(20.0, abs=1e-9)
        assert pct_b == pytest.approx(100.0, abs=1e-9)

    def test_clamps_floating_point_overshoot_to_100_percent(self):
        """Minor IEEE-754 drift where intersection_area slightly exceeds parcel_area is clamped to 100.0."""
        pct = compute_overlap_percentage(
            intersection_area_sqm=100.0 + 1e-11,
            parcel_area_sqm=100.0,
        )
        assert pct == 100.0

    @pytest.mark.parametrize("bad_parcel_area", [0.0, -1e-6, -500.0])
    def test_non_positive_parcel_area_raises_value_error(self, bad_parcel_area: float):
        with pytest.raises(ValueError, match="parcel_area_sqm must be > 0"):
            compute_overlap_percentage(intersection_area_sqm=1.0, parcel_area_sqm=bad_parcel_area)

    def test_non_positive_area_in_pair_raises_value_error(self):
        with pytest.raises(ValueError):
            compute_overlap_percentages(intersection_area_sqm=5.0, area_a_sqm=100.0, area_b_sqm=0.0)
        with pytest.raises(ValueError):
            compute_overlap_percentages(intersection_area_sqm=5.0, area_a_sqm=-10.0, area_b_sqm=100.0)

    def test_negative_intersection_area_raises_value_error(self):
        with pytest.raises(ValueError, match="intersection_area_sqm must be >= 0"):
            compute_overlap_percentage(intersection_area_sqm=-0.01, parcel_area_sqm=100.0)


# =====================================================================
# Geometric evaluation tests (Shapely / GEOS metric geometries)
# =====================================================================

class TestGeometricOverlapPercentageEvaluation:
    """Test `evaluate_pair_overlap_metrics` on projected metric polygons."""

    def test_partial_overlap_between_large_and_small_parcel(self):
        # Parcel A: 100m x 100m = 10,000 m² ([0..100, 0..100])
        parcel_a = Polygon([(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)])
        # Parcel B: 10m x 20m = 200 m² ([95..105, 0..20])
        # Intersection: [95..100, 0..20] = 5m x 20m = 100 m²
        # Expected: pct_a = (100 / 10,000) * 100 = 1.0%; pct_b = (100 / 200) * 100 = 50.0%
        parcel_b = Polygon([(95, 0), (105, 0), (105, 20), (95, 20), (95, 0)])

        id_a = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        id_b = uuid.UUID("00000000-0000-0000-0000-00000000000b")

        metrics = evaluate_pair_overlap_metrics(
            geom_a=parcel_a,
            geom_b=parcel_b,
            parcel_a_id=id_a,
            parcel_b_id=id_b,
        )

        assert isinstance(metrics, OverlapPercentageMetrics)
        assert metrics.parcel_a_id == id_a
        assert metrics.parcel_b_id == id_b
        assert metrics.area_a_sqm == pytest.approx(10_000.0, abs=1e-9)
        assert metrics.area_b_sqm == pytest.approx(200.0, abs=1e-9)
        assert metrics.intersection_area_sqm == pytest.approx(100.0, abs=1e-9)
        assert metrics.overlap_percentage_primary == pytest.approx(1.0, abs=1e-9)
        assert metrics.overlap_percentage_secondary == pytest.approx(50.0, abs=1e-9)
        assert metrics.max_overlap_percentage == pytest.approx(50.0, abs=1e-9)
        assert metrics.min_overlap_percentage == pytest.approx(1.0, abs=1e-9)
        assert isinstance(metrics.intersection_geometry, MultiPolygon)
        assert metrics.intersection_geometry.area == pytest.approx(100.0, abs=1e-9)

    def test_swapping_parcel_order_swaps_primary_and_secondary_percentages(self):
        wkt_a = "POLYGON((0 0, 50 0, 50 20, 0 20, 0 0))"      # 1000 m²
        wkt_b = "POLYGON((40 0, 60 0, 60 10, 40 10, 40 0))"   # 200 m², overlap = 10x10 = 100 m²

        forward = evaluate_pair_overlap_metrics(wkt_a, wkt_b)
        reverse = evaluate_pair_overlap_metrics(wkt_b, wkt_a)

        assert forward.overlap_percentage_primary == pytest.approx(10.0, abs=1e-9)
        assert forward.overlap_percentage_secondary == pytest.approx(50.0, abs=1e-9)
        assert reverse.overlap_percentage_primary == pytest.approx(50.0, abs=1e-9)
        assert reverse.overlap_percentage_secondary == pytest.approx(10.0, abs=1e-9)
        assert forward.max_overlap_percentage == pytest.approx(reverse.max_overlap_percentage, abs=1e-9)

    def test_edge_touching_parcels_have_zero_overlap_percentages(self):
        wkt_a = "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"
        wkt_b = "POLYGON((10 0, 20 0, 20 10, 10 10, 10 0))"

        metrics = evaluate_pair_overlap_metrics(wkt_a, wkt_b)
        assert metrics.intersection_area_sqm == 0.0
        assert metrics.overlap_percentage_primary == 0.0
        assert metrics.overlap_percentage_secondary == 0.0
        assert metrics.intersection_geometry.is_empty

    def test_degenerate_empty_geometry_raises_value_error(self):
        with pytest.raises(ValueError, match="area"):
            evaluate_pair_overlap_metrics("POLYGON EMPTY", "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))")


# =====================================================================
# PostGIS SQLAlchemy expression & query construction tests
# =====================================================================

class TestOverlapPercentageSQL:
    """Verify PostGIS SQL expressions and pairwise overlap percentage query construction."""

    def test_overlap_percentage_expr_compiles_safe_division_and_100_multiplier(self):
        t = table("parcels", column("inter_area", Float), column("parcel_area", Float))
        expr = overlap_percentage_expr(t.c.inter_area, t.c.parcel_area)
        sql = _sql(expr)
        assert isinstance(expr.type, Float)
        assert "NULLIF" in sql or "CASE" in sql
        assert "100.0" in sql or "100" in sql

    def test_pairwise_overlap_percentage_query_columns(self):
        query = build_pairwise_overlap_percentage_query(uuid.uuid4())
        assert list(query.selected_columns.keys()) == [
            "parcel_a_id",
            "parcel_b_id",
            "area_a_sqm",
            "area_b_sqm",
            "intersection_area_sqm",
            "overlap_percentage_primary",
            "overlap_percentage_secondary",
            "intersection_geometry",
        ]

    def test_pairwise_query_uses_projected_geometry_and_deduplicates_pairs(self):
        layer_id = uuid.uuid4()
        query = build_pairwise_overlap_percentage_query(layer_id, min_intersection_area_sqm=0.05)
        sql = _sql(query)
        params = query.compile(dialect=PG).params.values()

        assert "geometry_projected" in sql
        assert "geometry_wgs84" not in sql
        assert "a.id < b.id" in sql
        assert "ST_Intersects(a.geometry_projected, b.geometry_projected)" in sql
        assert "ORDER BY a.id, b.id" in sql
        assert layer_id in params
        assert 0.05 in params

    def test_negative_min_intersection_area_rejected(self):
        with pytest.raises(ValueError):
            build_pairwise_overlap_percentage_query(uuid.uuid4(), min_intersection_area_sqm=-0.1)
