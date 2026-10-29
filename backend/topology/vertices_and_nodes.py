"""Duplicate vertex and dangling node detection for cadastral topology.

Implements directive Section 17 anomaly detection:

``DUPLICATE_VERTEX``
    Consecutive vertices closer than ``tolerance_m`` (default ``Settings.grid_tolerance_meters`` = 0.001 m) in any
    exterior/interior ring, plus exact repeats of the same vertex at non-adjacent ring positions (pinch points).
    The standard ring closure (last coordinate == first coordinate) is never reported.

``DANGLING_NODE``
    * ``SPIKE_VERTEX``: a polygon ring vertex whose two neighbours lie in the same direction (a zero-width antenna).
    * ``DEGREE_ONE_ENDPOINT``: a line-network endpoint touched by no other line endpoint or interior vertex.
    * ``UNSNAPPED_BOUNDARY_NODE``: a polygon vertex lying within ``(0, snap_tolerance_m]`` of another parcel's
      boundary without touching it (default ``Settings.snap_healing_tolerance_meters`` = 0.25 m).

All metric work uses projected geometries (``HarmonizedParcel.geometry_projected``, SRID 32643).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geometry
from shapely import wkt as shapely_wkt
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from sqlalchemy import Float, Integer, Select, and_, column, func, select, true
from sqlalchemy.orm import aliased

from backend.models.parcel import HarmonizedParcel
from backend.topology.intersection import PROJECTED_SRID

DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M: float = 0.001
DEFAULT_DANGLING_SNAP_TOLERANCE_M: float = 0.25

NODE_KIND_SPIKE = "SPIKE_VERTEX"
NODE_KIND_DEGREE_ONE = "DEGREE_ONE_ENDPOINT"
NODE_KIND_UNSNAPPED = "UNSNAPPED_BOUNDARY_NODE"

#: Relative tolerance on the normalized cross product when testing for a collinear reversal.
_SPIKE_CROSS_REL_EPS = 1e-9
#: Coordinate quantum used to match line-network nodes.
_NODE_KEY_DECIMALS = 9


@dataclass(frozen=True, slots=True)
class DuplicateVertexAnomaly:
    """A repeated vertex within a polygon ring."""

    parcel_id: uuid.UUID | None
    part_index: int
    ring_index: int
    vertex_index: int
    point: Point
    distance_m: float
    is_consecutive: bool


@dataclass(frozen=True, slots=True)
class DanglingNodeAnomaly:
    """A dangling node: spike vertex, degree-one endpoint, or unsnapped boundary vertex."""

    parcel_id: uuid.UUID | None
    secondary_parcel_id: uuid.UUID | None
    node_kind: str
    point: Point
    gap_or_overshoot_m: float


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _validate_duplicate_tolerance(tolerance_m: float) -> None:
    if not math.isfinite(tolerance_m) or tolerance_m < 0.0:
        raise ValueError("tolerance_m must be >= 0 and finite")


def _validate_snap_tolerance(snap_tolerance_m: float) -> None:
    if not math.isfinite(snap_tolerance_m) or snap_tolerance_m <= 0.0:
        raise ValueError("snap_tolerance_m must be > 0 and finite")


def _polygon_parts(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [p for p in geom.geoms if not p.is_empty]
    return []


def _rings(poly: Polygon) -> list[list[tuple[float, float]]]:
    """Return exterior then interior rings as open coordinate lists (closing coordinate removed)."""
    rings = [list(poly.exterior.coords)] + [list(r.coords) for r in poly.interiors]
    return [[(c[0], c[1]) for c in ring[:-1]] for ring in rings]


def _dist(p: tuple[float, float], q: tuple[float, float]) -> float:
    return math.hypot(p[0] - q[0], p[1] - q[1])


def detect_duplicate_vertices(
    geom: BaseGeometry | str,
    parcel_id: uuid.UUID | None = None,
    tolerance_m: float = DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M,
) -> list[DuplicateVertexAnomaly]:
    """Detect consecutive near-duplicate vertices and exact non-adjacent repeated vertices in polygon rings."""
    _validate_duplicate_tolerance(tolerance_m)
    anomalies: list[DuplicateVertexAnomaly] = []

    for part_index, poly in enumerate(_polygon_parts(_coerce_geometry(geom))):
        for ring_index, coords in enumerate(_rings(poly)):
            n = len(coords)
            if n < 2:
                continue

            # Consecutive duplicates (including the wrap-around pair last -> first).
            for i in range(1, n):
                d = _dist(coords[i - 1], coords[i])
                if d <= tolerance_m:
                    anomalies.append(
                        DuplicateVertexAnomaly(
                            parcel_id=parcel_id,
                            part_index=part_index,
                            ring_index=ring_index,
                            vertex_index=i,
                            point=Point(coords[i]),
                            distance_m=d,
                            is_consecutive=True,
                        )
                    )
            if n > 2:
                d_wrap = _dist(coords[-1], coords[0])
                if d_wrap <= tolerance_m:
                    anomalies.append(
                        DuplicateVertexAnomaly(
                            parcel_id=parcel_id,
                            part_index=part_index,
                            ring_index=ring_index,
                            vertex_index=n - 1,
                            point=Point(coords[-1]),
                            distance_m=d_wrap,
                            is_consecutive=True,
                        )
                    )

            # Exact repeats at non-adjacent positions (pinch points).
            last_seen: dict[tuple[float, float], int] = {}
            for j, coord in enumerate(coords):
                prev = last_seen.get(coord)
                if prev is not None:
                    adjacent = (j - prev == 1) or (prev == 0 and j == n - 1)
                    if not adjacent:
                        anomalies.append(
                            DuplicateVertexAnomaly(
                                parcel_id=parcel_id,
                                part_index=part_index,
                                ring_index=ring_index,
                                vertex_index=j,
                                point=Point(coord),
                                distance_m=0.0,
                                is_consecutive=False,
                            )
                        )
                last_seen[coord] = j

    anomalies.sort(key=lambda a: (a.part_index, a.ring_index, a.vertex_index))
    return anomalies


def detect_layer_duplicate_vertices(
    parcels: Sequence[tuple[uuid.UUID, BaseGeometry | str]],
    tolerance_m: float = DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M,
) -> list[DuplicateVertexAnomaly]:
    """Detect duplicate vertices across a layer, ordered by `parcel_id` then ring position."""
    _validate_duplicate_tolerance(tolerance_m)
    results: list[DuplicateVertexAnomaly] = []
    for parcel_id, geom in sorted(parcels, key=lambda item: item[0]):
        results.extend(detect_duplicate_vertices(geom, parcel_id=parcel_id, tolerance_m=tolerance_m))
    return results


def detect_polygon_spike_nodes(
    geom: BaseGeometry | str,
    parcel_id: uuid.UUID | None = None,
) -> list[DanglingNodeAnomaly]:
    """Detect zero-width spike vertices where a ring doubles back on itself."""
    spikes: list[DanglingNodeAnomaly] = []
    for poly in _polygon_parts(_coerce_geometry(geom)):
        for coords in _rings(poly):
            n = len(coords)
            if n < 3:
                continue
            for i in range(n):
                cur = coords[i]
                prev = coords[i - 1]
                nxt = coords[(i + 1) % n]
                ux, uy = prev[0] - cur[0], prev[1] - cur[1]
                vx, vy = nxt[0] - cur[0], nxt[1] - cur[1]
                lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
                if lu == 0.0 or lv == 0.0:
                    continue  # duplicate vertex, reported separately
                cross = ux * vy - uy * vx
                dot = ux * vx + uy * vy
                if dot > 0.0 and abs(cross) <= _SPIKE_CROSS_REL_EPS * lu * lv:
                    spikes.append(
                        DanglingNodeAnomaly(
                            parcel_id=parcel_id,
                            secondary_parcel_id=None,
                            node_kind=NODE_KIND_SPIKE,
                            point=Point(cur),
                            gap_or_overshoot_m=min(lu, lv),
                        )
                    )
    return spikes


def _node_key(coord: Sequence[float]) -> tuple[float, float]:
    return (round(coord[0], _NODE_KEY_DECIMALS), round(coord[1], _NODE_KEY_DECIMALS))


def _line_parts(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if not g.is_empty]
    return []


def _detect_degree_one_endpoints(
    lines: Sequence[tuple[uuid.UUID, BaseGeometry]],
) -> list[DanglingNodeAnomaly]:
    degree: dict[tuple[float, float], int] = {}
    endpoints: list[tuple[uuid.UUID, tuple[float, float], Sequence[float]]] = []

    for parcel_id, geom in lines:
        for line in _line_parts(geom):
            coords = list(line.coords)
            if len(coords) < 2:
                continue
            for idx, coord in enumerate(coords):
                key = _node_key(coord)
                is_end = idx == 0 or idx == len(coords) - 1
                degree[key] = degree.get(key, 0) + (1 if is_end else 2)
                if is_end:
                    endpoints.append((parcel_id, key, coord))

    anomalies = [
        DanglingNodeAnomaly(
            parcel_id=parcel_id,
            secondary_parcel_id=None,
            node_kind=NODE_KIND_DEGREE_ONE,
            point=Point(coord[0], coord[1]),
            gap_or_overshoot_m=0.0,
        )
        for parcel_id, key, coord in endpoints
        if degree[key] == 1
    ]
    return anomalies


def _detect_unsnapped_boundary_nodes(
    polygons: Sequence[tuple[uuid.UUID, BaseGeometry]],
    snap_tolerance_m: float,
) -> list[DanglingNodeAnomaly]:
    anomalies: list[DanglingNodeAnomaly] = []
    for id_a, geom_a in polygons:
        parts_a = _polygon_parts(geom_a)
        for id_b, geom_b in polygons:
            if id_a == id_b or geom_a.distance(geom_b) > snap_tolerance_m:
                continue
            boundary_b = geom_b.boundary
            for poly in parts_a:
                for coords in _rings(poly):
                    for coord in coords:
                        pt = Point(coord)
                        d = float(pt.distance(boundary_b))
                        if 0.0 < d <= snap_tolerance_m:
                            anomalies.append(
                                DanglingNodeAnomaly(
                                    parcel_id=id_a,
                                    secondary_parcel_id=id_b,
                                    node_kind=NODE_KIND_UNSNAPPED,
                                    point=pt,
                                    gap_or_overshoot_m=d,
                                )
                            )
    return anomalies


def detect_dangling_nodes(
    geometries: Sequence[tuple[uuid.UUID, BaseGeometry | str]],
    snap_tolerance_m: float = DEFAULT_DANGLING_SNAP_TOLERANCE_M,
) -> list[DanglingNodeAnomaly]:
    """Detect spike vertices, degree-one line endpoints and unsnapped boundary vertices.

    Results are sorted deterministically by `(parcel_id, node_kind, x, y)`.
    """
    _validate_snap_tolerance(snap_tolerance_m)
    normalized = sorted(
        ((pid, _coerce_geometry(g)) for pid, g in geometries),
        key=lambda item: item[0],
    )
    polygons = [(pid, g) for pid, g in normalized if _polygon_parts(g)]
    lines = [(pid, g) for pid, g in normalized if _line_parts(g)]

    anomalies: list[DanglingNodeAnomaly] = []
    for pid, geom in polygons:
        anomalies.extend(detect_polygon_spike_nodes(geom, parcel_id=pid))
    anomalies.extend(_detect_degree_one_endpoints(lines))
    anomalies.extend(_detect_unsnapped_boundary_nodes(polygons, snap_tolerance_m))

    anomalies.sort(key=lambda a: (str(a.parcel_id), a.node_kind, a.point.x, a.point.y))
    return anomalies


def has_repeated_points_expr(geom: Any, tolerance_m: float = DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M) -> Any:
    """Boolean SQL expression: `ST_NPoints(geom) > ST_NPoints(ST_RemoveRepeatedPoints(geom, tolerance_m))`."""
    _validate_duplicate_tolerance(tolerance_m)
    return func.ST_NPoints(geom, type_=Integer) > func.ST_NPoints(
        func.ST_RemoveRepeatedPoints(geom, tolerance_m), type_=Integer
    )


def build_duplicate_vertex_detection_query(
    layer_id: uuid.UUID,
    tolerance_m: float = DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M,
) -> Select:
    """Build the PostGIS query listing parcels of `layer_id` that contain repeated vertices."""
    _validate_duplicate_tolerance(tolerance_m)
    geom = HarmonizedParcel.geometry_projected
    total = func.ST_NPoints(geom, type_=Integer)
    deduped = func.ST_NPoints(func.ST_RemoveRepeatedPoints(geom, tolerance_m), type_=Integer)

    return (
        select(
            HarmonizedParcel.id.label("parcel_id"),
            HarmonizedParcel.parcel_identifier.label("parcel_identifier"),
            total.label("total_points"),
            deduped.label("deduped_points"),
            (total - deduped).label("duplicate_vertex_count"),
            geom.label("geometry_projected"),
        )
        .where(
            HarmonizedParcel.layer_id == layer_id,
            has_repeated_points_expr(geom, tolerance_m),
        )
        .order_by(HarmonizedParcel.id)
    )


def build_dangling_node_detection_query(
    layer_id: uuid.UUID,
    snap_tolerance_m: float = DEFAULT_DANGLING_SNAP_TOLERANCE_M,
) -> Select:
    """Build the PostGIS query listing vertices of parcel `a` within `(0, snap_tolerance_m]` of parcel `b`'s boundary."""
    _validate_snap_tolerance(snap_tolerance_m)
    a = aliased(HarmonizedParcel, name="a")
    b = aliased(HarmonizedParcel, name="b")

    dump = (
        func.ST_DumpPoints(a.geometry_projected)
        .table_valued(
            column("path"),
            column("geom", Geometry(geometry_type="POINT", srid=PROJECTED_SRID)),
        )
        .render_derived(name="dp")
        .lateral()
    )
    boundary_b = func.ST_Boundary(b.geometry_projected)
    distance = func.ST_Distance(dump.c.geom, boundary_b, type_=Float)

    return (
        select(
            a.id.label("parcel_a_id"),
            b.id.label("parcel_b_id"),
            distance.label("dangling_distance_m"),
            dump.c.geom.label("dangling_point"),
        )
        .select_from(a)
        .join(dump, true())
        .join(
            b,
            and_(
                a.id != b.id,
                func.ST_DWithin(dump.c.geom, boundary_b, snap_tolerance_m),
            ),
        )
        .where(
            a.layer_id == layer_id,
            b.layer_id == layer_id,
            distance > 0,
        )
        .order_by(a.id, b.id, distance)
    )

