"""Longest shared boundary identification for deterministic sliver healing.

A sliver is merged into the adjacent parcel that shares the longest linear boundary with it. All lengths are metric
and computed on projected geometries (``HarmonizedParcel.geometry_projected``, default EPSG:32643).

Shared boundary definition::

    shared_boundary(A, B) = linear part of  boundary(A) ∩ boundary(B)

Parcels that only touch at a point (corner contact) have a shared boundary length of 0 and are never selected.
Ties are broken deterministically by ascending neighbor parcel UUID.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geometry
from shapely import wkt as shapely_wkt
from shapely.geometry import GeometryCollection, LineString, MultiLineString
from shapely.geometry.base import BaseGeometry
from sqlalchemy import Float, Select, and_, func, literal_column, select
from sqlalchemy.orm import aliased

from backend.models.parcel import HarmonizedParcel
from backend.topology.intersection import PROJECTED_SRID

#: ST_CollectionExtract type code selecting linear geometries (1 = point, 2 = line, 3 = polygon).
_LINE_DIMENSION = 2


@dataclass(frozen=True, slots=True)
class SharedBoundaryCandidateInput:
    """A neighbor parcel considered for sliver assignment."""

    parcel_id: uuid.UUID
    geometry: BaseGeometry | str
    is_sliver: bool = False
    dispute_locked: bool = False


@dataclass(frozen=True, slots=True)
class SharedBoundaryMatch:
    """A neighbor and the metric length of the boundary it shares with the target parcel."""

    target_parcel_id: uuid.UUID
    neighbor_parcel_id: uuid.UUID
    shared_length_m: float
    shared_boundary_geometry: MultiLineString


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _collect_lines(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, (MultiLineString, GeometryCollection)):
        lines: list[LineString] = []
        for part in geom.geoms:
            lines.extend(_collect_lines(part))
        return lines
    return []


def extract_shared_boundary_lines(
    geom_a: BaseGeometry | str,
    geom_b: BaseGeometry | str,
) -> MultiLineString:
    """Return the linear part of `boundary(A) ∩ boundary(B)` as a MultiLineString (empty if only points touch)."""
    poly_a = _coerce_geometry(geom_a)
    poly_b = _coerce_geometry(geom_b)
    if poly_a.is_empty or poly_b.is_empty:
        return MultiLineString()
    shared = poly_a.boundary.intersection(poly_b.boundary)
    lines = [line for line in _collect_lines(shared) if line.length > 0.0]
    return MultiLineString(lines)


def compute_shared_boundary_length_m(
    geom_a: BaseGeometry | str,
    geom_b: BaseGeometry | str,
) -> float:
    """Return the metric length of the boundary shared by two projected geometries (0.0 if none)."""
    return float(extract_shared_boundary_lines(geom_a, geom_b).length)


def rank_shared_boundary_neighbors(
    target_parcel_id: uuid.UUID,
    target_geometry: BaseGeometry | str,
    candidates: Sequence[SharedBoundaryCandidateInput],
    *,
    exclude_slivers: bool = True,
    exclude_dispute_locked: bool = True,
) -> list[SharedBoundaryMatch]:
    """Rank eligible neighbors by shared boundary length (descending), ties by ascending parcel UUID.

    The target itself, other slivers (when `exclude_slivers`), dispute-locked parcels (when
    `exclude_dispute_locked`) and neighbors with zero shared boundary length are excluded.
    """
    target = _coerce_geometry(target_geometry)
    matches: list[SharedBoundaryMatch] = []
    for cand in candidates:
        if cand.parcel_id == target_parcel_id:
            continue
        if exclude_slivers and cand.is_sliver:
            continue
        if exclude_dispute_locked and cand.dispute_locked:
            continue
        lines = extract_shared_boundary_lines(target, cand.geometry)
        length_m = float(lines.length)
        if length_m <= 0.0:
            continue
        matches.append(
            SharedBoundaryMatch(
                target_parcel_id=target_parcel_id,
                neighbor_parcel_id=cand.parcel_id,
                shared_length_m=length_m,
                shared_boundary_geometry=lines,
            )
        )
    matches.sort(key=lambda m: (-m.shared_length_m, m.neighbor_parcel_id))
    return matches


def identify_longest_shared_boundary_neighbor(
    target_parcel_id: uuid.UUID,
    target_geometry: BaseGeometry | str,
    candidates: Sequence[SharedBoundaryCandidateInput],
    *,
    exclude_slivers: bool = True,
    exclude_dispute_locked: bool = True,
) -> SharedBoundaryMatch | None:
    """Return the eligible neighbor sharing the longest boundary with the target, or `None` if there is none."""
    ranked = rank_shared_boundary_neighbors(
        target_parcel_id,
        target_geometry,
        candidates,
        exclude_slivers=exclude_slivers,
        exclude_dispute_locked=exclude_dispute_locked,
    )
    return ranked[0] if ranked else None


def shared_boundary_geometry_expr(geom_a: Any, geom_b: Any) -> Any:
    """PostGIS expression for the linear shared boundary as a MultiLineString.

    ``ST_Multi(ST_CollectionExtract(ST_Intersection(ST_Boundary(a), ST_Boundary(b)), 2))``; a corner-only contact
    yields an empty MultiLineString.
    """
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_Intersection(func.ST_Boundary(geom_a), func.ST_Boundary(geom_b)),
            literal_column(str(_LINE_DIMENSION)),
        ),
        type_=Geometry(geometry_type="MULTILINESTRING", srid=PROJECTED_SRID),
    )


def shared_boundary_length_expr(geom_a: Any, geom_b: Any) -> Any:
    """PostGIS expression for the metric length of the shared boundary."""
    return func.ST_Length(shared_boundary_geometry_expr(geom_a, geom_b), type_=Float)


def build_longest_shared_border_query(
    layer_id: uuid.UUID,
    target_parcel_id: uuid.UUID,
    limit: int | None = None,
) -> Select:
    """Build the query ranking neighbors of `target_parcel_id` by shared boundary length.

    Neighbors are restricted to the same layer, exclude the target, other slivers and dispute-locked parcels, and
    must share a strictly positive boundary length. Order is `shared_length_m DESC, b.id ASC` (deterministic ties).

    Raises:
        ValueError: If `limit` is provided and is not positive.
    """
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0")

    a = aliased(HarmonizedParcel, name="a")
    b = aliased(HarmonizedParcel, name="b")

    shared_geom = shared_boundary_geometry_expr(a.geometry_projected, b.geometry_projected)
    shared_len = shared_boundary_length_expr(a.geometry_projected, b.geometry_projected)

    query = (
        select(
            a.id.label("target_parcel_id"),
            b.id.label("neighbor_parcel_id"),
            shared_len.label("shared_length_m"),
            shared_geom.label("shared_boundary_geometry"),
        )
        .select_from(a)
        .join(
            b,
            and_(
                b.id != a.id,
                func.ST_Intersects(a.geometry_projected, b.geometry_projected),
            ),
        )
        .where(
            a.id == target_parcel_id,
            a.layer_id == layer_id,
            b.layer_id == layer_id,
            b.is_sliver.is_(False),
            b.dispute_locked.is_(False),
            shared_len > 0,
        )
        .order_by(shared_len.desc(), b.id.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    return query
