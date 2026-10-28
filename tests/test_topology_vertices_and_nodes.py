"""Tests for duplicate vertex and dangling node detection (catch-up Step 80/150).

Validates directive Section 17 topological anomaly detection rules for:
1. ``DUPLICATE_VERTEX``:
   - Exact (`0.0` m) and near-duplicate (`<= tolerance_m`, default `0.001` m matching
     `Settings.grid_tolerance_meters`) consecutive vertices in polygon exterior/interior rings and MultiPolygons.
   - Standard ring closure (`coords[-1] == coords[0]`) is NEVER falsely flagged as a duplicate vertex.
   - Pinched non-adjacent repeated vertices within the same ring (`is_consecutive=False`).
2. ``DANGLING_NODE``:
   - Degenerate backtrack spike/antenna vertices on polygon rings (`node_kind="SPIKE_VERTEX"`).
   - Degree-1 dead-end terminal endpoints in boundary line networks (`node_kind="DEGREE_ONE_ENDPOINT"`).
   - Unsnapped T-junction / undershoot / overshoot parcel vertices within `(0, snap_tolerance_m]` (default `0.25` m
     matching `Settings.snap_healing_tolerance_meters`) of an adjacent parcel boundary
     (`node_kind="UNSNAPPED_BOUNDARY_NODE"`).
3. PostGIS SQLAlchemy expression and query construction (`has_repeated_points_expr`,
   `build_duplicate_vertex_detection_query`, `build_dangling_node_detection_query`) operating on
   `HarmonizedParcel.geometry_projected` (SRID 32643).
"""

from __future__ import annotations

import uuid

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from sqlalchemy import column, table
from sqlalchemy.dialects import postgresql

from backend.core.config import get_settings
from backend.topology.vertices_and_nodes import (
    DEFAULT_DANGLING_SNAP_TOLERANCE_M,
    DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M,
    DanglingNodeAnomaly,
    DuplicateVertexAnomaly,
    build_dangling_node_detection_query,
    build_duplicate_vertex_detection_query,
    detect_dangling_nodes,
    detect_duplicate_vertices,
    detect_layer_duplicate_vertices,
    detect_polygon_spike_nodes,
    has_repeated_points_expr,
)

PG = postgresql.dialect()


def _sql(element) -> str:
    return str(element.compile(dialect=PG))


# =====================================================================
# Duplicate vertex detection tests
# =====================================================================

class TestDuplicateVertexDetection:
    """Test `detect_duplicate_vertices` and `detect_layer_duplicate_vertices` on projected geometries."""

    def test_default_tolerances_match_core_settings(self):
        settings = get_settings()
        assert DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M == 0.001
        assert DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M == settings.grid_tolerance_meters
        assert DEFAULT_DANGLING_SNAP_TOLERANCE_M == 0.25
        assert DEFAULT_DANGLING_SNAP_TOLERANCE_M == settings.snap_healing_tolerance_meters

    def test_clean_polygon_has_zero_duplicate_vertices(self):
        """Standard closed ring (first == last coordinate) must not be flagged."""
        square = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        assert detect_duplicate_vertices(square) == []

    def test_detects_exact_consecutive_duplicate_vertex_on_exterior_ring(self):
        # Vertex (10, 0) is repeated consecutively at indices 1 and 2
        poly = Polygon(
            [(0.0, 0.0), (10.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
        )
        pid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        anomalies = detect_duplicate_vertices(poly, parcel_id=pid)

        assert len(anomalies) == 1
        dup = anomalies[0]
        assert isinstance(dup, DuplicateVertexAnomaly)
        assert dup.parcel_id == pid
        assert dup.part_index == 0
        assert dup.ring_index == 0
        assert dup.vertex_index == 2
        assert dup.point.equals(Point(10.0, 0.0))
        assert dup.distance_m == pytest.approx(0.0, abs=1e-12)
        assert dup.is_consecutive is True

    def test_detects_sub_millimeter_near_duplicate_within_tolerance(self):
        # (10.0, 0.0) followed by (10.0005, 0.0): separation = 0.0005m <= 0.001m default tolerance
        poly = Polygon(
            [(0.0, 0.0), (10.0, 0.0), (10.0005, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
        )
        anomalies = detect_duplicate_vertices(poly, tolerance_m=0.001)
        assert len(anomalies) == 1
        assert anomalies[0].distance_m == pytest.approx(0.0005, abs=1e-9)
        assert anomalies[0].is_consecutive is True

        # With strict 0.0 tolerance, 0.0005m is not flagged
        assert detect_duplicate_vertices(poly, tolerance_m=0.0) == []

    def test_detects_duplicate_vertex_in_interior_hole_and_multipolygon(self):
        shell = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0)]
        hole_with_dup = [(5.0, 5.0), (15.0, 5.0), (15.0, 5.0), (15.0, 15.0), (5.0, 15.0), (5.0, 5.0)]
        poly_with_hole = Polygon(shell, [hole_with_dup])
        second_part = Polygon(
            [(30.0, 0.0), (40.0, 0.0), (40.0, 10.0), (40.0, 10.0), (30.0, 10.0), (30.0, 0.0)]
        )
        mp = MultiPolygon([poly_with_hole, second_part])

        anomalies = detect_duplicate_vertices(mp)
        assert len(anomalies) == 2
        assert anomalies[0].part_index == 0
        assert anomalies[0].ring_index == 1
        assert anomalies[0].point.equals(Point(15.0, 5.0))
        assert anomalies[1].part_index == 1
        assert anomalies[1].ring_index == 0
        assert anomalies[1].point.equals(Point(40.0, 10.0))

    def test_detects_non_consecutive_pinched_ring_duplicate_vertex(self):
        # Bowtie / figure-8 touch at (5, 5) visited twice at non-adjacent indices
        pinched = Polygon(
            [(0.0, 0.0), (10.0, 0.0), (5.0, 5.0), (10.0, 10.0), (0.0, 10.0), (5.0, 5.0), (0.0, 0.0)]
        )
        anomalies = detect_duplicate_vertices(pinched)
        assert any(not a.is_consecutive and a.point.equals(Point(5.0, 5.0)) for a in anomalies)

    def test_detect_layer_duplicate_vertices_sorts_by_parcel_id(self):
        id_b = uuid.UUID("00000000-0000-0000-0000-00000000000b")
        id_a = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        parcels = [
            (id_b, "POLYGON((0 0, 10 0, 10 0, 10 10, 0 10, 0 0))"),
            (id_a, "POLYGON((0 0, 5 0, 5 5, 5 5, 0 5, 0 0))"),
        ]
        results = detect_layer_duplicate_vertices(parcels)
        assert [r.parcel_id for r in results] == [id_a, id_b]

    def test_negative_tolerance_raises_value_error(self):
        square = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        with pytest.raises(ValueError, match="tolerance_m"):
            detect_duplicate_vertices(square, tolerance_m=-0.001)


# =====================================================================
# Dangling node detection tests
# =====================================================================

class TestDanglingNodeDetection:
    """Test `detect_polygon_spike_nodes` and `detect_dangling_nodes` across spike, endpoint, and T-junction cases."""

    def test_detects_degenerate_spike_vertex_on_polygon(self):
        # Square [0..10, 0..10] with a 1D antenna spike shooting out from (10, 5) to (13, 5) and back to (10, 5)
        spiked = Polygon(
            [
                (0.0, 0.0),
                (10.0, 0.0),
                (10.0, 5.0),
                (13.0, 5.0),
                (10.0, 5.0),
                (10.0, 10.0),
                (0.0, 10.0),
                (0.0, 0.0),
            ]
        )
        pid = uuid.UUID("00000000-0000-0000-0000-000000000010")
        spikes = detect_polygon_spike_nodes(spiked, parcel_id=pid)

        assert len(spikes) == 1
        spike = spikes[0]
        assert isinstance(spike, DanglingNodeAnomaly)
        assert spike.parcel_id == pid
        assert spike.node_kind == "SPIKE_VERTEX"
        assert spike.point.equals(Point(13.0, 5.0))
        assert spike.gap_or_overshoot_m == pytest.approx(3.0, abs=1e-9)

    def test_detects_degree_one_dead_end_endpoints_in_line_network(self):
        id_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
        # Closed square boundary loop + a dangling spur segment from (10, 5) to (12, 5)
        closed_ring = LineString([(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        spur = LineString([(10.0, 5.0), (12.0, 5.0)])

        dangles = detect_dangling_nodes([(id_1, closed_ring), (id_2, spur)])
        degree_one = [d for d in dangles if d.node_kind == "DEGREE_ONE_ENDPOINT"]
        assert len(degree_one) == 1
        assert degree_one[0].parcel_id == id_2
        assert degree_one[0].point.equals(Point(12.0, 5.0))

    def test_detects_unsnapped_boundary_node_between_adjacent_parcels(self):
        id_a = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        id_b = uuid.UUID("00000000-0000-0000-0000-00000000000b")

        # Parcel A: [0..10, 0..10]
        # Parcel B: shares (10, 0) and (10, 10), but has an unsnapped T-junction vertex at (10.12, 5.0)
        # Distance from (10.12, 5.0) to Parcel A's east edge is 0.12m <= 0.25m snap_tolerance_m
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        poly_b = Polygon(
            [(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0), (10.12, 5.0), (10.0, 0.0)]
        )

        dangles = detect_dangling_nodes([(id_a, poly_a), (id_b, poly_b)], snap_tolerance_m=0.25)
        unsnapped = [d for d in dangles if d.node_kind == "UNSNAPPED_BOUNDARY_NODE"]
        assert len(unsnapped) == 1
        assert unsnapped[0].parcel_id == id_b
        assert unsnapped[0].secondary_parcel_id == id_a
        assert unsnapped[0].point.equals(Point(10.12, 5.0))
        assert unsnapped[0].gap_or_overshoot_m == pytest.approx(0.12, abs=1e-9)

    def test_flush_shared_vertices_produce_zero_dangling_nodes(self):
        id_a = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        id_b = uuid.UUID("00000000-0000-0000-0000-00000000000b")
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        poly_b = Polygon([(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0), (10.0, 0.0)])

        assert detect_dangling_nodes([(id_a, poly_a), (id_b, poly_b)], snap_tolerance_m=0.25) == []

    def test_invalid_snap_tolerance_raises_value_error(self):
        id_a = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        poly_a = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)])
        with pytest.raises(ValueError, match="snap_tolerance_m"):
            detect_dangling_nodes([(id_a, poly_a)], snap_tolerance_m=0.0)


# =====================================================================
# PostGIS SQLAlchemy expression & query construction tests
# =====================================================================

class TestVerticesAndNodesSQL:
    """Verify PostGIS SQL expressions and queries for duplicate vertices and dangling nodes."""

    def test_has_repeated_points_expr_uses_st_remove_repeated_points_and_st_npoints(self):
        t = table("harmonized_parcels", column("geometry_projected"))
        expr = has_repeated_points_expr(t.c.geometry_projected, tolerance_m=0.001)
        sql = _sql(expr)
        assert "ST_NPoints(harmonized_parcels.geometry_projected)" in sql
        assert "ST_RemoveRepeatedPoints(harmonized_parcels.geometry_projected" in sql

    def test_build_duplicate_vertex_detection_query_columns_and_scoping(self):
        layer_id = uuid.uuid4()
        query = build_duplicate_vertex_detection_query(layer_id, tolerance_m=0.001)
        assert list(query.selected_columns.keys()) == [
            "parcel_id",
            "parcel_identifier",
            "total_points",
            "deduped_points",
            "duplicate_vertex_count",
            "geometry_projected",
        ]
        sql = _sql(query)
        params = query.compile(dialect=PG).params.values()

        assert "geometry_projected" in sql
        assert "geometry_wgs84" not in sql
        assert "ST_RemoveRepeatedPoints" in sql
        assert "ORDER BY harmonized_parcels.id" in sql
        assert layer_id in params

    def test_build_dangling_node_detection_query_columns_and_scoping(self):
        layer_id = uuid.uuid4()
        query = build_dangling_node_detection_query(layer_id, snap_tolerance_m=0.25)
        assert list(query.selected_columns.keys()) == [
            "parcel_a_id",
            "parcel_b_id",
            "dangling_distance_m",
            "dangling_point",
        ]
        sql = _sql(query)
        params = query.compile(dialect=PG).params.values()

        assert "geometry_projected" in sql
        assert "geometry_wgs84" not in sql
        assert "ST_DWithin(" in sql
        assert "ST_Boundary(" in sql
        assert "ORDER BY a.id, b.id" in sql
        assert layer_id in params
        assert 0.25 in params

    def test_sql_builders_reject_invalid_tolerances(self):
        with pytest.raises(ValueError):
            build_duplicate_vertex_detection_query(uuid.uuid4(), tolerance_m=-0.01)
        with pytest.raises(ValueError):
            build_dangling_node_detection_query(uuid.uuid4(), snap_tolerance_m=0.0)

