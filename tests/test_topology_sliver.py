"""Tests for metric sliver detection using area and Polsby-Popper compactness thresholds (catch-up Step 76/150).

Validates directive Section 16 sliver classification rules:
A polygon in projected metric coordinates (EPSG:32643) is a topological sliver if and only if BOTH conditions hold:
1. ``0 < area_sqm < max_area_sqm`` (default ``10.0`` m²)
2. ``0 < polsby_popper < max_polsby_popper`` (default ``0.05``)

Key invariants verified:
- Conjunction requirement:
  - Small compact plot (e.g., 2m x 2m = 4 m², PP = pi/4 ~ 0.7854 >= 0.05) -> NOT a sliver.
  - Large elongated corridor (e.g., 2m x 500m = 1000 m², PP ~ 0.0125 < 0.05, area >= 10 m²) -> NOT a sliver.
  - Thin boundary digitization artifact (e.g., 0.1m x 50m = 5 m², PP ~ 0.00626 < 0.05) -> IS a sliver.
- Strict `<` boundary condition at exact threshold values (`area_sqm == 10.0` or `polsby_popper == 0.05` is False).
- Custom threshold overrides and strict `ValueError` rejection for invalid thresholds or negative metrics.
- Batch layer evaluation (`detect_layer_slivers`) with deterministic `parcel_id` sorting.
- PostGIS SQLAlchemy query construction (`sliver_predicate_expr`, `build_sliver_detection_query`) using
  `geometry_projected` exclusively.
"""

from __future__ import annotations

import uuid

import pytest
from shapely.geometry import Polygon
from sqlalchemy import column, table
from sqlalchemy.dialects import postgresql

from backend.topology.sliver import (
    DEFAULT_SLIVER_MAX_AREA_SQM,
    DEFAULT_SLIVER_MAX_POLSBY_POPPER,
    SliverEvaluationResult,
    build_sliver_detection_query,
    detect_layer_slivers,
    evaluate_geometry_sliver,
    is_sliver_by_metrics,
    sliver_predicate_expr,
)

PG = postgresql.dialect()


def _sql(element) -> str:
    return str(element.compile(dialect=PG))


# =====================================================================
# Metric threshold classification tests
# =====================================================================

class TestSliverMetricClassification:
    """Test `is_sliver_by_metrics` across the 2x2 (area x compactness) truth table and boundary conditions."""

    def test_default_constants_match_directive_section_16(self):
        assert DEFAULT_SLIVER_MAX_AREA_SQM == 10.0
        assert DEFAULT_SLIVER_MAX_POLSBY_POPPER == 0.05

    def test_true_when_both_area_and_compactness_below_thresholds(self):
        assert is_sliver_by_metrics(area_sqm=4.5, polsby_popper=0.012) is True

    def test_false_for_small_compact_parcel(self):
        """A 2m x 2m utility plot (area=4 m², PP=0.785) is small but compact -> NOT a sliver."""
        assert is_sliver_by_metrics(area_sqm=4.0, polsby_popper=0.785398) is False

    def test_false_for_large_elongated_corridor(self):
        """A 2m x 500m road/canal strip (area=1000 m², PP=0.0125) is thin but large -> NOT a sliver."""
        assert is_sliver_by_metrics(area_sqm=1000.0, polsby_popper=0.0125) is False

    def test_false_for_large_compact_parcel(self):
        assert is_sliver_by_metrics(area_sqm=500.0, polsby_popper=0.75) is False

    def test_strict_inequality_at_exact_thresholds(self):
        """Exact equality to max_area_sqm (10.0) or max_polsby_popper (0.05) is NOT a sliver."""
        assert is_sliver_by_metrics(area_sqm=10.0, polsby_popper=0.01) is False
        assert is_sliver_by_metrics(area_sqm=5.0, polsby_popper=0.05) is False
        assert is_sliver_by_metrics(area_sqm=10.0, polsby_popper=0.05) is False

    def test_zero_area_or_zero_compactness_is_not_classified_as_polygonal_sliver(self):
        assert is_sliver_by_metrics(area_sqm=0.0, polsby_popper=0.01) is False
        assert is_sliver_by_metrics(area_sqm=2.0, polsby_popper=0.0) is False

    def test_custom_thresholds_override_defaults(self):
        # Under default (10.0, 0.05), (15.0, 0.08) is False; under relaxed (20.0, 0.10), it is True
        assert is_sliver_by_metrics(area_sqm=15.0, polsby_popper=0.08) is False
        assert (
            is_sliver_by_metrics(
                area_sqm=15.0,
                polsby_popper=0.08,
                max_area_sqm=20.0,
                max_polsby_popper=0.10,
            )
            is True
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"area_sqm": -1.0, "polsby_popper": 0.02},
            {"area_sqm": 5.0, "polsby_popper": -0.01},
            {"area_sqm": 5.0, "polsby_popper": 1.05},
            {"area_sqm": 5.0, "polsby_popper": 0.02, "max_area_sqm": 0.0},
            {"area_sqm": 5.0, "polsby_popper": 0.02, "max_area_sqm": -5.0},
            {"area_sqm": 5.0, "polsby_popper": 0.02, "max_polsby_popper": 0.0},
            {"area_sqm": 5.0, "polsby_popper": 0.02, "max_polsby_popper": 1.5},
        ],
    )
    def test_invalid_metrics_or_thresholds_raise_value_error(self, kwargs):
        with pytest.raises(ValueError):
            is_sliver_by_metrics(**kwargs)


# =====================================================================
# Geometric sliver evaluation & layer batch detection tests
# =====================================================================

class TestGeometricSliverDetection:
    """Test `evaluate_geometry_sliver` and `detect_layer_slivers` on projected polygons."""

    def test_thin_ribbon_polygon_detected_as_sliver(self):
        # 0.1m wide x 50m long ribbon: area = 5.0 m² (< 10.0), perimeter = 100.2 m, PP ~ 0.00626 (< 0.05)
        ribbon = Polygon([(0.0, 0.0), (50.0, 0.0), (50.0, 0.1), (0.0, 0.1), (0.0, 0.0)])
        pid = uuid.UUID("00000000-0000-0000-0000-000000000011")

        res = evaluate_geometry_sliver(ribbon, parcel_id=pid)
        assert isinstance(res, SliverEvaluationResult)
        assert res.parcel_id == pid
        assert res.area_sqm == pytest.approx(5.0, abs=1e-9)
        assert res.perimeter_m == pytest.approx(100.2, abs=1e-9)
        assert res.polsby_popper < 0.05
        assert res.is_sliver is True

    def test_small_square_plot_not_detected_as_sliver(self):
        # 2m x 2m square: area = 4.0 m² (< 10.0), perimeter = 8.0 m, PP = pi/4 ~ 0.7854 (>= 0.05)
        small_square = Polygon([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)])
        res = evaluate_geometry_sliver(small_square)
        assert res.area_sqm == pytest.approx(4.0, abs=1e-9)
        assert res.polsby_popper == pytest.approx(0.785398163, abs=1e-6)
        assert res.is_sliver is False

    def test_long_canal_parcel_not_detected_as_sliver(self):
        # 2m x 200m canal: area = 400.0 m² (>= 10.0), perimeter = 404.0 m, PP ~ 0.0308 (< 0.05)
        canal = Polygon([(0.0, 0.0), (200.0, 0.0), (200.0, 2.0), (0.0, 2.0), (0.0, 0.0)])
        res = evaluate_geometry_sliver(canal)
        assert res.area_sqm == pytest.approx(400.0, abs=1e-9)
        assert res.polsby_popper < 0.05
        assert res.is_sliver is False

    def test_detect_layer_slivers_filters_and_sorts_deterministically(self):
        id_sliver_high = uuid.UUID("00000000-0000-0000-0000-000000000099")
        id_normal = uuid.UUID("00000000-0000-0000-0000-000000000050")
        id_sliver_low = uuid.UUID("00000000-0000-0000-0000-000000000005")

        parcels = [
            (id_sliver_high, "POLYGON((0 0, 40 0, 40 0.1, 0 0.1, 0 0))"),    # 4.0 m², PP ~ 0.0078 -> sliver
            (id_normal, "POLYGON((0 0, 20 0, 20 20, 0 20, 0 0))"),           # 400 m², PP ~ 0.785 -> normal
            (id_sliver_low, "POLYGON((0 0, 30 0, 30 0.05, 0 0.05, 0 0))"),   # 1.5 m², PP ~ 0.0052 -> sliver
        ]

        slivers = detect_layer_slivers(parcels)
        assert [s.parcel_id for s in slivers] == [id_sliver_low, id_sliver_high]
        assert all(s.is_sliver for s in slivers)


# =====================================================================
# PostGIS SQLAlchemy expression & query construction tests
# =====================================================================

class TestSliverDetectionSQL:
    """Verify `sliver_predicate_expr` and `build_sliver_detection_query` SQL compilation."""

    def test_sliver_predicate_expr_combines_area_and_compactness_conditions(self):
        t = table("harmonized_parcels", column("geometry_projected"))
        pred = sliver_predicate_expr(t.c.geometry_projected, max_area_sqm=10.0, max_polsby_popper=0.05)
        sql = _sql(pred)
        assert "ST_Area(harmonized_parcels.geometry_projected)" in sql
        assert "ST_Perimeter(harmonized_parcels.geometry_projected)" in sql

    def test_build_sliver_detection_query_columns_and_scoping(self):
        layer_id = uuid.uuid4()
        query = build_sliver_detection_query(
            layer_id=layer_id,
            max_area_sqm=8.5,
            max_polsby_popper=0.04,
        )
        assert list(query.selected_columns.keys()) == [
            "parcel_id",
            "parcel_identifier",
            "area_sqm",
            "perimeter_m",
            "polsby_popper",
            "geometry_projected",
        ]
        sql = _sql(query)
        params = query.compile(dialect=PG).params.values()

        assert "geometry_projected" in sql
        assert "geometry_wgs84" not in sql
        assert "harmonized_parcels.layer_id =" in sql
        assert "ORDER BY harmonized_parcels.id" in sql
        assert layer_id in params
        assert 8.5 in params
        assert 0.04 in params

    def test_build_sliver_detection_query_rejects_invalid_thresholds(self):
        with pytest.raises(ValueError):
            build_sliver_detection_query(uuid.uuid4(), max_area_sqm=0.0)
        with pytest.raises(ValueError):
            build_sliver_detection_query(uuid.uuid4(), max_polsby_popper=0.0)
        with pytest.raises(ValueError):
            build_sliver_detection_query(uuid.uuid4(), max_polsby_popper=1.2)
