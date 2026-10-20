"""Tests for PostGIS intersection calculations used by cadastral overlap detection (catch-up for schedule Step 70).

Scope
-----
The production module under test is ``backend.topology.intersection``. It builds the PostGIS expressions and the
pairwise query that later steps (overlap detection, overlap percentages, severity classification) rely on.

Two layers of verification:

1. SQL-construction tests (always run). They compile the SQLAlchemy expressions with the PostgreSQL dialect and
   assert the exact PostGIS functions, the metric geometry column, pair de-duplication and layer scoping.
   Rule 14 of the directive: metric operations must use the projected CRS, never the WGS 84 column.

2. Live PostGIS tests (run only when ``POSTGIS_TEST_DATABASE_URL`` points to a PostGIS 3.x database, for example
   ``postgresql+asyncpg://user:pass@localhost:5432/db``). They execute the expressions in PostGIS and compare the
   results with analytically known areas. They are skipped otherwise, so a green local run without that variable
   does NOT prove the PostGIS behaviour; run them against the docker-compose ``postgres`` service.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import Float, column, func, select, table
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from backend.topology.intersection import (
    build_pairwise_intersection_query,
    intersection_area_expr,
    intersection_geometry_expr,
)

PG = postgresql.dialect()
POSTGIS_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")


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
# Live PostGIS verification (skipped unless POSTGIS_TEST_DATABASE_URL is set)
# =====================================================================

SQUARE_A = "POLYGON((0 0,10 0,10 10,0 10,0 0))"

LIVE_AREA_CASES = [
    pytest.param(SQUARE_A, "POLYGON((5 5,15 5,15 15,5 15,5 5))", 25.0, id="partial-overlap"),
    pytest.param(SQUARE_A, "POLYGON((2 2,7 2,7 7,2 7,2 2))", 25.0, id="fully-contained"),
    pytest.param(SQUARE_A, SQUARE_A, 100.0, id="identical"),
    pytest.param(SQUARE_A, "POLYGON((10 0,20 0,20 10,10 10,10 0))", 0.0, id="edge-touching"),
    pytest.param(SQUARE_A, "POLYGON((10 10,20 10,20 20,10 20,10 10))", 0.0, id="corner-touching"),
    pytest.param(SQUARE_A, "POLYGON((30 30,40 30,40 40,30 40,30 30))", 0.0, id="disjoint"),
]


@pytest_asyncio.fixture
async def postgis_conn():
    if not POSTGIS_URL:
        pytest.skip("POSTGIS_TEST_DATABASE_URL not set; live PostGIS verification skipped")
    engine = create_async_engine(POSTGIS_URL)
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(select(func.postgis_version()))).scalar_one()
            assert version, "PostGIS extension is not installed in the target database"
            yield conn
    finally:
        await engine.dispose()


def _geom(wkt: str):
    return func.ST_GeomFromText(wkt, 32643)


@pytest.mark.skipif(not POSTGIS_URL, reason="POSTGIS_TEST_DATABASE_URL not set")
class TestLivePostGISIntersection:
    """Executes the expressions in PostGIS and checks analytically known areas (SRID 32643, square meters)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("wkt_a, wkt_b, expected_area", LIVE_AREA_CASES)
    async def test_intersection_area_matches_analytic_value(self, postgis_conn, wkt_a, wkt_b, expected_area):
        area = (
            await postgis_conn.execute(select(intersection_area_expr(_geom(wkt_a), _geom(wkt_b))))
        ).scalar_one()
        assert area == pytest.approx(expected_area, abs=1e-9)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("wkt_a, wkt_b, expected_area", LIVE_AREA_CASES)
    async def test_intersection_area_is_symmetric(self, postgis_conn, wkt_a, wkt_b, expected_area):
        ab = (await postgis_conn.execute(select(intersection_area_expr(_geom(wkt_a), _geom(wkt_b))))).scalar_one()
        ba = (await postgis_conn.execute(select(intersection_area_expr(_geom(wkt_b), _geom(wkt_a))))).scalar_one()
        assert ab == pytest.approx(ba, abs=1e-9)

    @pytest.mark.asyncio
    async def test_intersection_geometry_is_multipolygon_for_overlap(self, postgis_conn):
        geom_type = (
            await postgis_conn.execute(
                select(
                    func.ST_GeometryType(
                        intersection_geometry_expr(_geom(SQUARE_A), _geom("POLYGON((5 5,15 5,15 15,5 15,5 5))"))
                    )
                )
            )
        ).scalar_one()
        assert geom_type == "ST_MultiPolygon"

    @pytest.mark.asyncio
    async def test_intersection_geometry_is_empty_for_edge_touching(self, postgis_conn):
        is_empty = (
            await postgis_conn.execute(
                select(
                    func.ST_IsEmpty(
                        intersection_geometry_expr(
                            _geom(SQUARE_A), _geom("POLYGON((10 0,20 0,20 10,10 10,10 0))")
                        )
                    )
                )
            )
        ).scalar_one()
        assert is_empty is True
