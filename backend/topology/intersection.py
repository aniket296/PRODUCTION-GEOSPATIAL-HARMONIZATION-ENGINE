"""PostGIS intersection expressions, pairwise overlap-candidate query, and deterministic metric evaluation.

All metric work uses ``HarmonizedParcel.geometry_projected`` (default UTM Zone 43N, EPSG:32643). The WGS 84
column is never used for areas or distances (directive rule 14).

Definitions (directive section 15)::

    intersection_area = area(A ∩ B)

Two parcels that only share an edge or a corner intersect on a line or a point. That intersection has zero area and
is NOT an overlap, so the query and evaluator require a strictly positive intersection area.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geometry
from shapely import wkt as shapely_wkt
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from sqlalchemy import Float, Select, and_, func, literal_column, select
from sqlalchemy.orm import aliased

from backend.models.parcel import HarmonizedParcel

#: ST_CollectionExtract type code selecting polygonal geometries (1 = point, 2 = line, 3 = polygon).
_POLYGON_DIMENSION = 3

#: SRID of ``HarmonizedParcel.geometry_projected``.
PROJECTED_SRID = 32643


@dataclass(frozen=True, slots=True)
class OverlapCandidate:
    """Deterministic pairwise overlap candidate between two parcels in the same layer."""

    parcel_a_id: uuid.UUID
    parcel_b_id: uuid.UUID
    intersection_area_sqm: float
    intersection_geometry: MultiPolygon


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    """Coerce a Shapely geometry or WKT string into a BaseGeometry."""
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def extract_polygonal_intersection(
    geom_a: BaseGeometry | str,
    geom_b: BaseGeometry | str,
) -> MultiPolygon:
    """Return the polygonal intersection of two projected geometries as a normalized MultiPolygon.

    Matches ``ST_Multi(ST_CollectionExtract(ST_Intersection(geom_a, geom_b), 3))`` semantics:
    line/point boundary contacts are discarded, returning an empty ``MultiPolygon`` when no
    two-dimensional overlap exists.
    """
    poly_a = _coerce_geometry(geom_a)
    poly_b = _coerce_geometry(geom_b)
    raw = poly_a.intersection(poly_b)
    if raw.is_empty:
        return MultiPolygon()
    if isinstance(raw, Polygon):
        return MultiPolygon([raw])
    if isinstance(raw, MultiPolygon):
        return raw
    if isinstance(raw, GeometryCollection):
        polygons: list[Polygon] = []
        for part in raw.geoms:
            if isinstance(part, Polygon) and not part.is_empty:
                polygons.append(part)
            elif isinstance(part, MultiPolygon) and not part.is_empty:
                polygons.extend(p for p in part.geoms if not p.is_empty)
        return MultiPolygon(polygons)
    return MultiPolygon()


def compute_intersection_area_sqm(
    geom_a: BaseGeometry | str,
    geom_b: BaseGeometry | str,
) -> float:
    """Compute ``area(A ∩ B)`` in square meters for projected metric geometries."""
    return float(extract_polygonal_intersection(geom_a, geom_b).area)


def detect_pairwise_overlaps(
    parcels: Sequence[tuple[uuid.UUID, BaseGeometry | str]],
    min_intersection_area_sqm: float = 0.0,
) -> list[OverlapCandidate]:
    """Deterministically evaluate pairwise overlaps for a collection of ``(parcel_id, projected_geom)`` records.

    Unordered pairs are deduplicated with ``parcel_a_id < parcel_b_id`` and returned sorted by
    ``(parcel_a_id, parcel_b_id)``, matching ``build_pairwise_intersection_query``.
    """
    if min_intersection_area_sqm < 0:
        raise ValueError("min_intersection_area_sqm must be >= 0")

    normalized: list[tuple[uuid.UUID, BaseGeometry]] = sorted(
        ((pid, _coerce_geometry(geom)) for pid, geom in parcels),
        key=lambda item: item[0],
    )
    results: list[OverlapCandidate] = []
    n = len(normalized)
    for i in range(n):
        id_a, geom_a = normalized[i]
        for j in range(i + 1, n):
            id_b, geom_b = normalized[j]
            if id_a >= id_b:
                continue
            if not geom_a.intersects(geom_b):
                continue
            overlap_geom = extract_polygonal_intersection(geom_a, geom_b)
            area_sqm = float(overlap_geom.area)
            if area_sqm > min_intersection_area_sqm:
                results.append(
                    OverlapCandidate(
                        parcel_a_id=id_a,
                        parcel_b_id=id_b,
                        intersection_area_sqm=area_sqm,
                        intersection_geometry=overlap_geom,
                    )
                )
    return results


def intersection_area_expr(geom_a: Any, geom_b: Any) -> Any:
    """Return ``ST_Area(ST_Intersection(geom_a, geom_b))`` in the units of the geometries' CRS (m² for UTM)."""
    return func.ST_Area(func.ST_Intersection(geom_a, geom_b), type_=Float)


def intersection_geometry_expr(geom_a: Any, geom_b: Any) -> Any:
    """Return the polygonal part of ``ST_Intersection`` as a MultiPolygon.

    ``ST_Intersection`` of polygons may yield a GeometryCollection holding lines or points where boundaries touch.
    ``ST_CollectionExtract(..., 3)`` keeps only the polygonal parts and ``ST_Multi`` normalizes the result, so an
    edge-touching pair yields an empty MultiPolygon.
    """
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_Intersection(geom_a, geom_b),
            literal_column(str(_POLYGON_DIMENSION)),
        ),
        type_=Geometry(geometry_type="MULTIPOLYGON", srid=PROJECTED_SRID),
    )


def build_pairwise_intersection_query(
    layer_id: uuid.UUID,
    min_intersection_area_sqm: float = 0.0,
) -> Select:
    """Build the query listing every overlapping parcel pair of one layer.

    Each unordered pair is reported once (``a.id < b.id``) in deterministic ``(a.id, b.id)`` order. ``ST_Intersects``
    in the join condition lets PostGIS use the GiST index on ``geometry_projected``; the area predicate then removes
    pairs that only touch.

    Args:
        layer_id: Layer whose parcels are compared; both sides are restricted to it.
        min_intersection_area_sqm: Intersections with an area less than or equal to this are excluded. The default
            of 0 excludes touching-only pairs.

    Raises:
        ValueError: If ``min_intersection_area_sqm`` is negative.
    """
    if min_intersection_area_sqm < 0:
        raise ValueError("min_intersection_area_sqm must be >= 0")

    a = aliased(HarmonizedParcel, name="a")
    b = aliased(HarmonizedParcel, name="b")

    return (
        select(
            a.id.label("parcel_a_id"),
            b.id.label("parcel_b_id"),
            intersection_area_expr(a.geometry_projected, b.geometry_projected).label("intersection_area_sqm"),
            intersection_geometry_expr(a.geometry_projected, b.geometry_projected).label("intersection_geometry"),
        )
        .select_from(a)
        .join(
            b,
            and_(
                a.id < b.id,
                func.ST_Intersects(a.geometry_projected, b.geometry_projected),
            ),
        )
        .where(
            a.layer_id == layer_id,
            b.layer_id == layer_id,
            intersection_area_expr(a.geometry_projected, b.geometry_projected) > min_intersection_area_sqm,
        )
        .order_by(a.id, b.id)
    )
