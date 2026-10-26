"""Tests for cadastral boundary gap detection using the configured metric tolerance (catch-up Step 78/150).

Validates directive boundary gap detection rules:
1. Default metric gap tolerance is ``0.5`` meters (`DEFAULT_BOUNDARY_GAP_TOLERANCE_M = 0.5`), matching
   `Settings.boundary_gap_tolerance_meters` in `backend.core.config`.
2. Two adjacent parcels separated by a narrow void ``0 < d <= tolerance_m`` (or touching at endpoints with an
   inter-boundary crack/void within `tolerance_m`) produce a non-empty polygonal `MultiPolygon` gap geometry with
   strictly positive `gap_area_sqm`.
3. Flush edge-touching parcels (`d == 0` with no crack) and parcels separated by more than `tolerance_m` (e.g.,
   across a public road corridor) produce no gap.
4. Pairwise layer scanning (`detect_layer_boundary_gaps`) deduplicates unordered pairs (`parcel_a_id < parcel_b_id`)
   and orders results deterministically.
5. PostGIS SQLAlchemy query construction (`pairwise_gap_geometry_expr`, `build_boundary_gap_detection_query`)
   uses `ST_DWithin`, `ST_Difference`, `ST_Buffer`, `ST_Union`, and `ST_CollectionExtract(..., 3)` on
   `geometry_projected` (SRID 32643) exclusively.
"""

from __future__ import annotations

import uuid

import pytest
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import column, table
from sqlalchemy.dialects import postgresql

from backend.core.config import get_settings
from backend.topology.gap import (
    DEFAULT_BOUNDARY_GAP_TOLERANCE_M,
    BoundaryGapCandidate,
    build_boundary_gap_detection_query,
    detect_layer_boundary_gaps,
    evaluate_pairwise_boundary_gap,
    extract_pairwise_gap_geometry,
    pairwise_gap_geometry_expr,
)

PG = postgresql.dialect()


def _sql(element) -> str:
    return str(element.compile(dialect=PG))


# =====================================================================
# Configuration & geometric gap extraction tests
# =====================================================================

class TestBoundaryGapGeometryExtraction:
    """Test `extract_pairwise_gap_geometry` and `evaluate_pairwise_boundary_gap` across metric tolerances."""

    def test_default_tolerance_matches_core_settings(self):
        settings = get_settings()
        assert DEFAULT_BOUNDARY_GAP_TOLERANCE_M == 0.5
        assert DEFAULT_BOUNDARY_GAP_TOLERANCE_M == settings.boundary_gap_tolerance_meters

    def test_detects_parallel_strip_gap_within_default_tolerance(self):
        # Parcel A: [0..10, 0..10]; Parcel B: [10.3..20.3, 0..10]
        # Separation = 0.3m <= 0.5m tolerance along a 10m shared frontage -> 3.0 m² rectangular gap
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        poly_b = Polygon([(10.3, 0.0), (20.3, 0.0), (20.3, 10.0), (10.3, 10.0), (10.3, 0.0)])

        gap_geom = extract_pairwise_gap_geometry(poly_a, poly_b, tolerance_m=0.5)
        assert isinstance(gap_geom, MultiPolygon)
        assert not gap_geom.is_empty
        assert gap_geom.area == pytest.approx(3.0, rel=0.02)

        id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
        candidate = evaluate_pairwise_boundary_gap(
            geom_a=poly_a,
            geom_b=poly_b,
            parcel_a_id=id_a,
            parcel_b_id=id_b,
            tolerance_m=0.5,
        )
        assert isinstance(candidate, BoundaryGapCandidate)
        assert candidate.parcel_a_id == id_a
        assert candidate.parcel_b_id == id_b
        assert candidate.min_distance_m == pytest.approx(0.3, abs=1e-9)
        assert candidate.gap_area_sqm == pytest.approx(3.0, rel=0.02)
        assert candidate.tolerance_m == 0.5

    def test_ignores_separation_exceeding_configured_tolerance(self):
        # Separation = 1.2m > 0.5m default tolerance -> no micro-gap
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        poly_b = Polygon([(11.2, 0.0), (21.2, 0.0), (21.2, 10.0), (11.2, 10.0), (11.2, 0.0)])

        gap_geom = extract_pairwise_gap_geometry(poly_a, poly_b, tolerance_m=0.5)
        assert gap_geom.is_empty
        assert evaluate_pairwise_boundary_gap(poly_a, poly_b, tolerance_m=0.5) is None

        # When tolerance is explicitly increased to 1.5m, the 1.2m x 10m = 12.0 m² gap IS detected
        wider_candidate = evaluate_pairwise_boundary_gap(poly_a, poly_b, tolerance_m=1.5)
        assert wider_candidate is not None
        assert wider_candidate.gap_area_sqm == pytest.approx(12.0, rel=0.02)

    def test_flush_edge_touching_parcels_produce_no_gap(self):
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        poly_b = Polygon([(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0), (10.0, 0.0)])

        gap_geom = extract_pairwise_gap_geometry(poly_a, poly_b, tolerance_m=0.5)
        assert gap_geom.is_empty
        assert evaluate_pairwise_boundary_gap(poly_a, poly_b, tolerance_m=0.5) is None

    def test_detects_pinched_interior_crack_between_touching_parcels(self):
        # Parcel A and B touch at (10, 0) and (10, 10), but B's shared edge bows east by 0.2m at y=5
        # Triangle crack has base = 10m, height = 0.2m -> area = 0.5 * 10 * 0.2 = 1.0 m²
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        poly_b = Polygon([(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0), (10.2, 5.0), (10.0, 0.0)])

        candidate = evaluate_pairwise_boundary_gap(poly_a, poly_b, tolerance_m=0.5)
        assert candidate is not None
        assert candidate.min_distance_m == pytest.approx(0.0, abs=1e-9)
        assert candidate.gap_area_sqm == pytest.approx(1.0, rel=0.05)

    @pytest.mark.parametrize("bad_tol", [0.0, -0.5, float("nan"), float("inf")])
    def test_invalid_tolerance_raises_value_error(self, bad_tol: float):
        poly = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        with pytest.raises(ValueError, match="tolerance_m"):
            extract_pairwise_gap_geometry(poly, poly, tolerance_m=bad_tol)

    def test_negative_min_gap_area_raises_value_error(self):
        poly = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        with pytest.raises(ValueError, match="min_gap_area_sqm"):
            evaluate_pairwise_boundary_gap(poly, poly, tolerance_m=0.5, min_gap_area_sqm=-0.1)


# =====================================================================
# Layer-wide pairwise gap detection tests
# =====================================================================

class TestLayerBoundaryGapDetection:
    """Test `detect_layer_boundary_gaps` pair deduplication and deterministic sorting."""

    def test_detect_layer_boundary_gaps_orders_and_deduplicates_pairs(self):
        id_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
        id_3 = uuid.UUID("00000000-0000-0000-0000-000000000003")

        # id_1 and id_2 have a 0.25m gap (area = 2.5 m²); id_2 and id_3 are flush touching (no gap)
        parcels = [
            (id_3, "POLYGON((20.25 0, 30.25 0, 30.25 10, 20.25 10, 20.25 0))"),
            (id_1, "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"),
            (id_2, "POLYGON((10.25 0, 20.25 0, 20.25 10, 10.25 10, 10.25 0))"),
        ]

        gaps = detect_layer_boundary_gaps(parcels, tolerance_m=0.5)
        assert len(gaps) == 1
        assert gaps[0].parcel_a_id == id_1
        assert gaps[0].parcel_b_id == id_2
        assert gaps[0].min_distance_m == pytest.approx(0.25, abs=1e-9)
        assert gaps[0].gap_area_sqm == pytest.approx(2.5, rel=0.02)


# =====================================================================
# PostGIS SQLAlchemy expression & query construction tests
# =====================================================================

class TestBoundaryGapSQL:
    """Verify `pairwise_gap_geometry_expr` and `build_boundary_gap_detection_query` SQL compilation."""

    def test_pairwise_gap_geometry_expr_uses_morphological_closing_difference(self):
        a = table("a", column("geometry_projected"))
        b = table("b", column("geometry_projected"))
        expr = pairwise_gap_geometry_expr(a.c.geometry_projected, b.c.geometry_projected, tolerance_m=0.5)
        sql = _sql(expr)
        assert "ST_Multi(" in sql
        assert "ST_CollectionExtract(" in sql
        assert "ST_Difference(" in sql
        assert "ST_Buffer(" in sql
        assert "ST_Union(" in sql
        assert ", 3)" in sql

    def test_build_boundary_gap_detection_query_columns_and_predicates(self):
        layer_id = uuid.uuid4()
        query = build_boundary_gap_detection_query(
            layer_id=layer_id,
            tolerance_m=0.5,
            min_gap_area_sqm=0.01,
        )
        assert list(query.selected_columns.keys()) == [
            "parcel_a_id",
            "parcel_b_id",
            "min_distance_m",
            "gap_area_sqm",
            "gap_geometry",
        ]
        sql = _sql(query)
        params = query.compile(dialect=PG).params.values()

        assert "geometry_projected" in sql
        assert "geometry_wgs84" not in sql
        assert "ST_DWithin(a.geometry_projected, b.geometry_projected" in sql
        assert "a.id < b.id" in sql
        assert "ORDER BY a.id, b.id" in sql
        assert layer_id in params
        assert 0.5 in params
        assert 0.01 in params

    def test_build_boundary_gap_detection_query_rejects_invalid_arguments(self):
        with pytest.raises(ValueError):
            build_boundary_gap_detection_query(uuid.uuid4(), tolerance_m=0.0)
        with pytest.raises(ValueError):
            build_boundary_gap_detection_query(uuid.uuid4(), tolerance_m=-1.0)
        with pytest.raises(ValueError):
            build_boundary_gap_detection_query(uuid.uuid4(), min_gap_area_sqm=-0.5)
