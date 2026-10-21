"""Tests for PostGIS intersection calculations and overlap detection (catch-up Steps 70-71).

Scope
-----
The production module under test is ``backend.topology.intersection``. It builds the PostGIS expressions, the
pairwise overlap query, and deterministic metric evaluation helpers that subsequent steps (overlap percentages,
severity classification) rely on.

Verification layers:
1. SQL-construction tests: compile SQLAlchemy expressions with the PostgreSQL dialect and assert exact PostGIS
   functions (`ST_Area`, `ST_Intersection`, `ST_Multi`, `ST_CollectionExtract`, `ST_Intersects`), the metric
   geometry column (`geometry_projected`), pair deduplication (`a.id < b.id`), and layer scoping.
2. Analytic geometric intersection tests: verify `compute_intersection_area_sqm`, `extract_polygonal_intersection`,
   and `detect_pairwise_overlaps` against analytically known areas, boundary/corner touches, MultiPolygon
   normalization, and deterministic pair ordering.
"""

from __future__ import annotations

import uuid

import pytest
from shapely.geometry import MultiPolygon
from sqlalchemy import Float, column, func, table
from sqlalchemy.dialects import postgresql

from backend.topology.intersection import (
    OverlapCandidate,
    build_pairwise_intersection_query,
    compute_intersection_area_sqm,
    detect_pairwise_overlaps,
    extract_polygonal_intersection,
    intersection_area_expr,
    intersection_geometry_expr,
)

PG = postgresql.dialect()


def _sql(element) -> str:
    return str(element.compile(dialect=PG))


@pytest.fixture
def geom_cols():
    a = table("a", column("geometry_projected"))
    b = table("b", column("geometry_projected"))
    return a.c.geometry_projected, b.c.geometry_projected


# =====================================================================
# SQL construction: expressions
# =====================================================================

class TestIntersectionExpressions:
    """The expression helpers must emit the documented PostGIS function composition."""

    def test_intersection_area_expression_sql(self, geom_cols):
        expr = intersection_area_expr(*geom_cols)
        assert _sql(expr) == "ST_Area(ST_Intersection(a.geometry_projected, b.geometry_projected))"

    def test_intersection_area_expression_is_float(self, geom_cols):
        expr = intersection_area_expr(*geom_cols)
        assert isinstance(expr.type, Float)

    def test_intersection_geometry_keeps_only_polygonal_parts(self, geom_cols):
        """ST_Intersection can return lines/points for touching parcels; only polygonal parts are overlap."""
        sql = _sql(intersection_geometry_expr(*geom_cols))
        assert "ST_Multi(" in sql
        assert "ST_CollectionExtract(" in sql
        assert "ST_Intersection(a.geometry_projected, b.geometry_projected)" in sql
        # 3 = polygon dimension for ST_CollectionExtract
        assert ", 3)" in sql

    def test_expressions_accept_arbitrary_geometry_elements(self):
        expr = intersection_area_expr(
            func.ST_GeomFromText("POLYGON((0 0,1 0,1 1,0 1,0 0))", 32643),
            func.ST_GeomFromText("POLYGON((0 0,1 0,1 1,0 1,0 0))", 32643),
        )
        assert "ST_Area(ST_Intersection(ST_GeomFromText(" in _sql(expr)


# =====================================================================
# SQL construction: pairwise query
# =====================================================================

class TestPairwiseIntersectionQuery:
    """Pairwise parcel intersection query semantics."""

    def test_selected_columns_are_stable_contract(self):
        query = build_pairwise_intersection_query(uuid.uuid4())
        assert list(query.selected_columns.keys()) == [
            "parcel_a_id",
            "parcel_b_id",
            "intersection_area_sqm",
            "intersection_geometry",
        ]

    def test_uses_projected_metric_geometry_only(self):
        sql = _sql(build_pairwise_intersection_query(uuid.uuid4()))
        assert "geometry_projected" in sql
        assert "geometry_wgs84" not in sql

    def test_scoped_to_a_single_layer_on_both_sides(self):
        layer_id = uuid.uuid4()
        query = build_pairwise_intersection_query(layer_id)
        sql = _sql(query)
        assert "a.layer_id =" in sql
        assert "b.layer_id =" in sql
        assert layer_id in query.compile(dialect=PG).params.values()

    def test_each_unordered_pair_appears_once_in_deterministic_order(self):
        sql = _sql(build_pairwise_intersection_query(uuid.uuid4()))
        assert "a.id < b.id" in sql
        assert "ORDER BY a.id, b.id" in sql

    def test_uses_index_friendly_intersects_predicate(self):
        sql = _sql(build_pairwise_intersection_query(uuid.uuid4()))
        assert "ST_Intersects(a.geometry_projected, b.geometry_projected)" in sql

    def test_requires_strictly_positive_intersection_area(self):
        """Edge-touching parcels intersect on a line (area 0) and must not be reported as overlaps."""
        sql = _sql(build_pairwise_intersection_query(uuid.uuid4()))
        assert "ST_Area(ST_Intersection(a.geometry_projected, b.geometry_projected)) >" in sql

    def test_minimum_area_threshold_is_bound_parameter(self):
        query = build_pairwise_intersection_query(uuid.uuid4(), min_intersection_area_sqm=0.25)
        assert 0.25 in query.compile(dialect=PG).params.values()

    def test_negative_minimum_area_rejected(self):
        with pytest.raises(ValueError):
            build_pairwise_intersection_query(uuid.uuid4(), min_intersection_area_sqm=-1.0)


# =====================================================================
# Analytic geometric intersection & pairwise overlap evaluation
# =====================================================================

SQUARE_A = "POLYGON((0 0,10 0,10 10,0 10,0 0))"

ANALYTIC_AREA_CASES = [
    pytest.param(SQUARE_A, "POLYGON((5 5,15 5,15 15,5 15,5 5))", 25.0, id="partial-overlap"),
    pytest.param(SQUARE_A, "POLYGON((2 2,7 2,7 7,2 7,2 2))", 25.0, id="fully-contained"),
    pytest.param(SQUARE_A, SQUARE_A, 100.0, id="identical"),
    pytest.param(SQUARE_A, "POLYGON((10 0,20 0,20 10,10 10,10 0))", 0.0, id="edge-touching"),
    pytest.param(SQUARE_A, "POLYGON((10 10,20 10,20 20,10 20,10 10))", 0.0, id="corner-touching"),
    pytest.param(SQUARE_A, "POLYGON((30 30,40 30,40 40,30 40,30 30))", 0.0, id="disjoint"),
]


class TestAnalyticIntersectionEvaluation:
    """Executes deterministic metric intersection evaluation and checks analytically known areas (SRID 32643, m²)."""

    @pytest.mark.parametrize("wkt_a, wkt_b, expected_area", ANALYTIC_AREA_CASES)
    def test_intersection_area_matches_analytic_value(self, wkt_a, wkt_b, expected_area):
        area = compute_intersection_area_sqm(wkt_a, wkt_b)
        assert area == pytest.approx(expected_area, abs=1e-9)

    @pytest.mark.parametrize("wkt_a, wkt_b, expected_area", ANALYTIC_AREA_CASES)
    def test_intersection_area_is_symmetric(self, wkt_a, wkt_b, expected_area):
        ab = compute_intersection_area_sqm(wkt_a, wkt_b)
        ba = compute_intersection_area_sqm(wkt_b, wkt_a)
        assert ab == pytest.approx(ba, abs=1e-9)

    def test_intersection_geometry_is_multipolygon_for_overlap(self):
        geom = extract_polygonal_intersection(SQUARE_A, "POLYGON((5 5,15 5,15 15,5 15,5 5))")
        assert isinstance(geom, MultiPolygon)
        assert not geom.is_empty
        assert geom.area == pytest.approx(25.0, abs=1e-9)

    def test_intersection_geometry_is_empty_for_edge_touching(self):
        geom = extract_polygonal_intersection(SQUARE_A, "POLYGON((10 0,20 0,20 10,10 10,10 0))")
        assert isinstance(geom, MultiPolygon)
        assert geom.is_empty

    def test_detect_pairwise_overlaps_deduplicates_and_excludes_touching(self):
        id_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
        id_3 = uuid.UUID("00000000-0000-0000-0000-000000000003")

        # Pass out-of-order to verify deterministic sorting by (parcel_a_id, parcel_b_id)
        parcels = [
            (id_3, "POLYGON((10 0,20 0,20 10,10 10,10 0))"),  # touches id_1 on x=10, overlaps id_2
            (id_1, SQUARE_A),
            (id_2, "POLYGON((5 5,15 5,15 15,5 15,5 5))"),
        ]
        overlaps = detect_pairwise_overlaps(parcels)
        assert len(overlaps) == 2
        assert overlaps[0] == OverlapCandidate(
            parcel_a_id=id_1,
            parcel_b_id=id_2,
            intersection_area_sqm=pytest.approx(25.0, abs=1e-9),
            intersection_geometry=overlaps[0].intersection_geometry,
        )
        assert overlaps[1].parcel_a_id == id_2
        assert overlaps[1].parcel_b_id == id_3
        assert overlaps[1].intersection_area_sqm == pytest.approx(25.0, abs=1e-9)
