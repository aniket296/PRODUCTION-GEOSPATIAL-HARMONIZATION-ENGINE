"""Tests for the Polsby-Popper compactness formula and PostGIS expression helper (catch-up Step 74/150).

Validates directive Section 16 mathematical definition::

    Polsby-Popper = (4 * pi * Area) / (Perimeter ** 2)

Key invariants verified:
1. Canonical analytic values:
   - Circle: PP = 1.0
   - Square: PP = pi / 4 (~0.785398)
   - Equilateral triangle: PP = (pi * sqrt(3)) / 9 (~0.604600)
   - Thin cadastral sliver (0.1m x 50m): PP ~ 0.006258 (< 0.05 sliver threshold)
2. Scale and translation invariance in projected metric coordinates (SRID 32643).
3. Safe zero-division handling (0 area or 0 perimeter -> 0.0), clamping to [0.0, 1.0], and strict ValueError
   rejection for negative or non-finite inputs.
4. Geometric evaluation (`evaluate_geometry_compactness`, `CompactnessMetrics`) on Polygon, MultiPolygon,
   polygons with interior rings (holes), and empty geometries.
5. PostGIS SQLAlchemy expression compilation (`polsby_popper_expr`) for both geometry columns (`ST_Area`,
   `ST_Perimeter`) and explicit `(area, perimeter)` numeric columns.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point, Polygon
from sqlalchemy import Float, column, table
from sqlalchemy.dialects import postgresql

from backend.topology.compactness import (
    CompactnessMetrics,
    compute_polsby_popper,
    evaluate_geometry_compactness,
    polsby_popper_expr,
)

PG = postgresql.dialect()


def _sql(element) -> str:
    return str(element.compile(dialect=PG))


# =====================================================================
# Pure mathematical formula tests
# =====================================================================

class TestPolsbyPopperFormula:
    """Verify `compute_polsby_popper` against closed-form geometric identities and edge cases."""

    def test_ideal_circle_equals_one(self):
        radius = 25.0
        area = math.pi * radius * radius
        perimeter = 2.0 * math.pi * radius
        assert compute_polsby_popper(area, perimeter) == pytest.approx(1.0, abs=1e-12)

    def test_square_equals_pi_over_four(self):
        side = 40.0
        area = side * side
        perimeter = 4.0 * side
        assert compute_polsby_popper(area, perimeter) == pytest.approx(math.pi / 4.0, abs=1e-12)

    def test_equilateral_triangle_matches_closed_form(self):
        side = 30.0
        area = (math.sqrt(3.0) / 4.0) * side * side
        perimeter = 3.0 * side
        expected = (math.pi * math.sqrt(3.0)) / 9.0
        assert compute_polsby_popper(area, perimeter) == pytest.approx(expected, abs=1e-12)

    def test_elongated_sliver_falls_well_below_0_05_threshold(self):
        # 0.1m x 50m ribbon: area = 5.0 m², perimeter = 100.2 m
        area = 5.0
        perimeter = 100.2
        expected = (4.0 * math.pi * 5.0) / (100.2 * 100.2)
        pp = compute_polsby_popper(area, perimeter)
        assert pp == pytest.approx(expected, abs=1e-12)
        assert pp < 0.05

    @pytest.mark.parametrize("scale", [0.1, 1.0, 7.5, 1000.0])
    def test_scale_invariance(self, scale: float):
        base_w, base_h = 12.0, 35.0
        area = (base_w * scale) * (base_h * scale)
        perimeter = 2.0 * ((base_w * scale) + (base_h * scale))
        expected = compute_polsby_popper(base_w * base_h, 2.0 * (base_w + base_h))
        assert compute_polsby_popper(area, perimeter) == pytest.approx(expected, abs=1e-12)

    def test_zero_area_or_zero_perimeter_returns_zero_safely(self):
        assert compute_polsby_popper(0.0, 50.0) == 0.0
        assert compute_polsby_popper(100.0, 0.0) == 0.0
        assert compute_polsby_popper(0.0, 0.0) == 0.0

    def test_clamps_floating_point_overshoot_above_one(self):
        # Isoperimetric inequality guarantees PP <= 1.0; tiny IEEE-754 drift above 1.0 is clamped to 1.0
        perimeter = 2.0 * math.pi
        area = math.pi * (1.0 + 1e-15)
        assert compute_polsby_popper(area, perimeter) == 1.0

    @pytest.mark.parametrize(
        "bad_area, bad_perimeter",
        [
            (-1.0, 10.0),
            (10.0, -1.0),
            (float("nan"), 10.0),
            (10.0, float("nan")),
            (float("inf"), 10.0),
            (10.0, float("inf")),
        ],
    )
    def test_negative_or_non_finite_inputs_raise_value_error(self, bad_area: float, bad_perimeter: float):
        with pytest.raises(ValueError):
            compute_polsby_popper(bad_area, bad_perimeter)


# =====================================================================
# Geometric evaluation tests (Shapely / GEOS metric geometries)
# =====================================================================

class TestGeometryCompactnessEvaluation:
    """Verify `evaluate_geometry_compactness` on projected geometries and WKT strings."""

    def test_unit_square_geometry_metrics(self):
        square = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
        metrics = evaluate_geometry_compactness(square)
        assert isinstance(metrics, CompactnessMetrics)
        assert metrics.area_sqm == pytest.approx(100.0, abs=1e-9)
        assert metrics.perimeter_m == pytest.approx(40.0, abs=1e-9)
        assert metrics.polsby_popper == pytest.approx(math.pi / 4.0, abs=1e-9)

    def test_high_resolution_buffered_circle_approaches_one(self):
        circle_approx = Point(500_000.0, 2_500_000.0).buffer(50.0, quad_segs=128)
        metrics = evaluate_geometry_compactness(circle_approx)
        assert metrics.polsby_popper == pytest.approx(1.0, abs=1e-4)

    def test_polygon_with_interior_hole_reduces_compactness(self):
        shell = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]
        hole = [(5, 5), (15, 5), (15, 15), (5, 15), (5, 5)]
        solid = Polygon(shell)
        perforated = Polygon(shell, [hole])

        solid_metrics = evaluate_geometry_compactness(solid)
        perforated_metrics = evaluate_geometry_compactness(perforated)

        # Perforated has area = 400 - 100 = 300 m² and perimeter = 80 + 40 = 120 m
        assert perforated_metrics.area_sqm == pytest.approx(300.0, abs=1e-9)
        assert perforated_metrics.perimeter_m == pytest.approx(120.0, abs=1e-9)
        assert perforated_metrics.polsby_popper < solid_metrics.polsby_popper
        assert perforated_metrics.polsby_popper == pytest.approx(
            (4.0 * math.pi * 300.0) / (120.0 * 120.0),
            abs=1e-9,
        )

    def test_accepts_wkt_string_and_handles_empty_geometry(self):
        wkt_metrics = evaluate_geometry_compactness("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))")
        assert wkt_metrics.polsby_popper == pytest.approx(math.pi / 4.0, abs=1e-9)

        empty_metrics = evaluate_geometry_compactness("POLYGON EMPTY")
        assert empty_metrics == CompactnessMetrics(area_sqm=0.0, perimeter_m=0.0, polsby_popper=0.0)


# =====================================================================
# PostGIS SQLAlchemy expression tests
# =====================================================================

class TestPolsbyPopperSQL:
    """Verify `polsby_popper_expr` SQL compilation for geometry and numeric column inputs."""

    def test_geometry_column_expression_uses_st_area_and_st_perimeter(self):
        t = table("harmonized_parcels", column("geometry_projected"))
        expr = polsby_popper_expr(t.c.geometry_projected)
        sql = _sql(expr)
        assert isinstance(expr.type, Float)
        assert "ST_Area(harmonized_parcels.geometry_projected)" in sql
        assert "ST_Perimeter(harmonized_parcels.geometry_projected)" in sql
        assert "NULLIF" in sql or "CASE" in sql

    def test_area_and_perimeter_column_expression_compiles_safe_formula(self):
        t = table("harmonized_parcels", column("metric_area_sqm", Float), column("metric_perimeter_m", Float))
        expr = polsby_popper_expr(t.c.metric_area_sqm, t.c.metric_perimeter_m)
        sql = _sql(expr)
        assert isinstance(expr.type, Float)
        assert "harmonized_parcels.metric_area_sqm" in sql
        assert "harmonized_parcels.metric_perimeter_m" in sql
        assert "NULLIF" in sql or "CASE" in sql
