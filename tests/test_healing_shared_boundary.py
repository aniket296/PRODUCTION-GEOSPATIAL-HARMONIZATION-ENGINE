"""Tests for longest shared boundary identification used in deterministic sliver healing (Step 88/150).

Validates:
1. Metric shared boundary extraction and length calculation (`extract_shared_boundary_lines`,
   `compute_shared_boundary_length_m`) in projected coordinates (EPSG:32643):
   - Single and multi-segment shared edges return exact linear length in meters (`MultiLineString`).
   - Corner-touching (0D point intersection) and disjoint polygons return `0.0` m and an empty `MultiLineString`.
2. Longest shared boundary neighbor selection (`identify_longest_shared_boundary_neighbor`,
   `rank_shared_boundary_neighbors`, `SharedBoundaryMatch`):
   - Selects the adjacent parcel sharing the longest linear boundary with the target sliver.
   - Deterministic tie-breaking by ascending `neighbor_parcel_id` when two neighbors share identical boundary lengths.
   - Excludes self (`neighbor_id == target_parcel_id`), dispute-locked parcels (`dispute_locked=True`), and other
     sliver parcels (`is_sliver=True`).
   - Returns `None` when the target parcel is isolated or only touches neighbors at a 0D corner point.
3. PostGIS SQLAlchemy expression and query construction (`shared_boundary_geometry_expr`,
   `shared_boundary_length_expr`, `build_longest_shared_border_query`) using `geometry_projected` (SRID 32643),
   `ST_Boundary`, `ST_Intersection`, `ST_CollectionExtract(..., 2)`, `ST_Length`, and deterministic
   `ORDER BY shared_length_m DESC, b.id ASC`.
"""

from __future__ import annotations

import uuid

import pytest
from shapely.geometry import MultiLineString, Polygon
from sqlalchemy import Float, column, table
from sqlalchemy.dialects import postgresql

from backend.healing.shared_boundary import (
    SharedBoundaryCandidateInput,
    SharedBoundaryMatch,
    build_longest_shared_border_query,
    compute_shared_boundary_length_m,
    extract_shared_boundary_lines,
    identify_longest_shared_boundary_neighbor,
    rank_shared_boundary_neighbors,
    shared_boundary_geometry_expr,
    shared_boundary_length_expr,
)

PG = postgresql.dialect()


def _sql(element) -> str:
    return str(element.compile(dialect=PG))


# =====================================================================
# Metric shared boundary length & linear extraction tests
# =====================================================================

class TestSharedBoundaryCalculations:
    """Verify `extract_shared_boundary_lines` and `compute_shared_boundary_length_m` on projected polygons."""

    def test_single_shared_edge_returns_exact_length_and_multilinestring(self):
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 25.0), (0.0, 25.0), (0.0, 0.0)])
        poly_b = Polygon([(10.0, 0.0), (30.0, 0.0), (30.0, 25.0), (10.0, 25.0), (10.0, 0.0)])

        lines = extract_shared_boundary_lines(poly_a, poly_b)
        length_m = compute_shared_boundary_length_m(poly_a, poly_b)

        assert isinstance(lines, MultiLineString)
        assert not lines.is_empty
        assert lines.length == pytest.approx(25.0, abs=1e-9)
        assert length_m == pytest.approx(25.0, abs=1e-9)

    def test_partial_edge_contact_returns_overlapping_segment_length(self):
        # poly_a east edge is y in [0, 20]; poly_b west edge is y in [5, 17] -> 12.0 m shared border
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 20.0), (0.0, 20.0), (0.0, 0.0)])
        poly_b = Polygon([(10.0, 5.0), (20.0, 5.0), (20.0, 17.0), (10.0, 17.0), (10.0, 5.0)])

        assert compute_shared_boundary_length_m(poly_a, poly_b) == pytest.approx(12.0, abs=1e-9)
        assert compute_shared_boundary_length_m(poly_b, poly_a) == pytest.approx(12.0, abs=1e-9)

    def test_l_shaped_multi_segment_shared_border_sums_both_legs(self):
        # Inner square [0..10, 0..10] cradled on its east (10m) and north (10m) edges by an L-shaped neighbor -> 20m
        inner = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        l_neighbor = Polygon(
            [
                (10.0, 0.0),
                (20.0, 0.0),
                (20.0, 20.0),
                (0.0, 20.0),
                (0.0, 10.0),
                (10.0, 10.0),
                (10.0, 0.0),
            ]
        )
        assert compute_shared_boundary_length_m(inner, l_neighbor) == pytest.approx(20.0, abs=1e-9)

    def test_corner_touch_and_disjoint_polygons_have_zero_shared_boundary_length(self):
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        corner_b = Polygon([(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0), (10.0, 10.0)])
        disjoint_c = Polygon([(50.0, 50.0), (60.0, 50.0), (60.0, 60.0), (50.0, 60.0), (50.0, 50.0)])

        assert compute_shared_boundary_length_m(poly_a, corner_b) == 0.0
        assert extract_shared_boundary_lines(poly_a, corner_b).is_empty
        assert compute_shared_boundary_length_m(poly_a, disjoint_c) == 0.0
        assert extract_shared_boundary_lines(poly_a, disjoint_c).is_empty


# =====================================================================
# Longest shared boundary neighbor selection & tie-breaking tests
# =====================================================================

class TestLongestSharedBoundaryNeighborSelection:
    """Verify `identify_longest_shared_boundary_neighbor` and `rank_shared_boundary_neighbors`."""

    def test_selects_neighbor_with_longest_shared_border(self):
        sliver_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
        # Sliver: 0.1m wide (x in [10.0, 10.1]), 40m tall (y in [0, 40])
        sliver = Polygon([(10.0, 0.0), (10.1, 0.0), (10.1, 40.0), (10.0, 40.0), (10.0, 0.0)])

        id_west_long = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_east_short = uuid.UUID("00000000-0000-0000-0000-000000000002")
        id_south_cap = uuid.UUID("00000000-0000-0000-0000-000000000003")

        # West parcel shares full 40m along x=10.0; East parcel shares 25m along x=10.1; South shares 0.1m along y=0
        west_poly = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 40.0), (0.0, 40.0), (0.0, 0.0)])
        east_poly = Polygon([(10.1, 0.0), (25.0, 0.0), (25.0, 25.0), (10.1, 25.0), (10.1, 0.0)])
        south_poly = Polygon([(10.0, -10.0), (10.1, -10.0), (10.1, 0.0), (10.0, 0.0), (10.0, -10.0)])

        candidates = [
            SharedBoundaryCandidateInput(parcel_id=id_east_short, geometry=east_poly),
            SharedBoundaryCandidateInput(parcel_id=id_south_cap, geometry=south_poly),
            SharedBoundaryCandidateInput(parcel_id=id_west_long, geometry=west_poly),
            SharedBoundaryCandidateInput(parcel_id=sliver_id, geometry=sliver, is_sliver=True),
        ]

        best = identify_longest_shared_boundary_neighbor(
            target_parcel_id=sliver_id,
            target_geometry=sliver,
            candidates=candidates,
        )
        assert isinstance(best, SharedBoundaryMatch)
        assert best.target_parcel_id == sliver_id
        assert best.neighbor_parcel_id == id_west_long
        assert best.shared_length_m == pytest.approx(40.0, abs=1e-9)

        ranked = rank_shared_boundary_neighbors(sliver_id, sliver, candidates)
        assert [m.neighbor_parcel_id for m in ranked] == [id_west_long, id_east_short, id_south_cap]

    def test_deterministic_tie_breaking_by_smallest_parcel_uuid(self):
        sliver_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
        sliver = Polygon([(10.0, 0.0), (10.1, 0.0), (10.1, 30.0), (10.0, 30.0), (10.0, 0.0)])

        id_lower = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        id_higher = uuid.UUID("00000000-0000-0000-0000-00000000000b")

        # Both west and east neighbors share an identical 30.0m border with the sliver
        west_poly = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 30.0), (0.0, 30.0), (0.0, 0.0)])
        east_poly = Polygon([(10.1, 0.0), (20.1, 0.0), (20.1, 30.0), (10.1, 30.0), (10.1, 0.0)])

        best_1 = identify_longest_shared_boundary_neighbor(
            sliver_id,
            sliver,
            [
                SharedBoundaryCandidateInput(parcel_id=id_higher, geometry=east_poly),
                SharedBoundaryCandidateInput(parcel_id=id_lower, geometry=west_poly),
            ],
        )
        best_2 = identify_longest_shared_boundary_neighbor(
            sliver_id,
            sliver,
            [
                SharedBoundaryCandidateInput(parcel_id=id_lower, geometry=west_poly),
                SharedBoundaryCandidateInput(parcel_id=id_higher, geometry=east_poly),
            ],
        )
        assert best_1 is not None and best_2 is not None
        assert best_1.neighbor_parcel_id == id_lower
        assert best_2.neighbor_parcel_id == id_lower

    def test_excludes_dispute_locked_and_sliver_candidates_by_default(self):
        sliver_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
        sliver = Polygon([(10.0, 0.0), (10.1, 0.0), (10.1, 40.0), (10.0, 40.0), (10.0, 0.0)])

        id_locked_west = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_eligible_east = uuid.UUID("00000000-0000-0000-0000-000000000002")

        # West shares 40m but is dispute-locked; East shares 25m and is unlocked -> East must be chosen
        west_locked = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 40.0), (0.0, 40.0), (0.0, 0.0)])
        east_unlocked = Polygon([(10.1, 0.0), (25.0, 0.0), (25.0, 25.0), (10.1, 25.0), (10.1, 0.0)])

        best = identify_longest_shared_boundary_neighbor(
            sliver_id,
            sliver,
            [
                SharedBoundaryCandidateInput(
                    parcel_id=id_locked_west,
                    geometry=west_locked,
                    dispute_locked=True,
                ),
                SharedBoundaryCandidateInput(
                    parcel_id=id_eligible_east,
                    geometry=east_unlocked,
                    dispute_locked=False,
                ),
            ],
        )
        assert best is not None
        assert best.neighbor_parcel_id == id_eligible_east
        assert best.shared_length_m == pytest.approx(25.0, abs=1e-9)

    def test_returns_none_when_no_neighbor_shares_positive_length_border(self):
        sliver_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
        sliver = Polygon([(10.0, 0.0), (10.1, 0.0), (10.1, 40.0), (10.0, 40.0), (10.0, 0.0)])
        far_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        far_poly = Polygon([(100.0, 100.0), (120.0, 100.0), (120.0, 120.0), (100.0, 120.0), (100.0, 100.0)])

        assert (
            identify_longest_shared_boundary_neighbor(
                sliver_id,
                sliver,
                [SharedBoundaryCandidateInput(parcel_id=far_id, geometry=far_poly)],
            )
            is None
        )


# =====================================================================
# PostGIS SQLAlchemy expression & query construction tests
# =====================================================================

class TestLongestSharedBorderSQL:
    """Verify PostGIS SQL expressions and `build_longest_shared_border_query`."""

    def test_shared_boundary_expressions_compile_linear_intersection_and_length(self):
        a = table("a", column("geometry_projected"))
        b = table("b", column("geometry_projected"))

        geom_expr = shared_boundary_geometry_expr(a.c.geometry_projected, b.c.geometry_projected)
        len_expr = shared_boundary_length_expr(a.c.geometry_projected, b.c.geometry_projected)

        geom_sql = _sql(geom_expr)
        len_sql = _sql(len_expr)

        assert "ST_Multi(" in geom_sql
        assert "ST_CollectionExtract(" in geom_sql
        assert "ST_Intersection(ST_Boundary(a.geometry_projected), ST_Boundary(b.geometry_projected))" in geom_sql
        assert ", 2)" in geom_sql

        assert isinstance(len_expr.type, Float)
        assert "ST_Length(" in len_sql
        assert "ST_Intersection(ST_Boundary(a.geometry_projected), ST_Boundary(b.geometry_projected))" in len_sql

    def test_build_longest_shared_border_query_columns_filters_and_deterministic_ordering(self):
        layer_id = uuid.uuid4()
        target_id = uuid.uuid4()
        query = build_longest_shared_border_query(layer_id=layer_id, target_parcel_id=target_id)

        assert list(query.selected_columns.keys()) == [
            "target_parcel_id",
            "neighbor_parcel_id",
            "shared_length_m",
            "shared_boundary_geometry",
        ]
        sql = _sql(query)
        params = query.compile(dialect=PG).params.values()

        assert "geometry_projected" in sql
        assert "geometry_wgs84" not in sql
        assert "ST_Intersects(a.geometry_projected, b.geometry_projected)" in sql
        assert "b.is_sliver IS false" in sql or "b.is_sliver =" in sql
        assert "b.dispute_locked IS false" in sql or "b.dispute_locked =" in sql
        assert "DESC" in sql
        assert "b.id ASC" in sql or "b.id" in sql
        assert layer_id in params
        assert target_id in params
