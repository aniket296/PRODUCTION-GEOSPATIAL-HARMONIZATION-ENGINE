"""Cadastral boundary gap detection using morphological closing and PostGIS query builders.

Implements directive Section 17 boundary gap detection rules:
1. Default metric gap tolerance is ``0.5`` meters (`DEFAULT_BOUNDARY_GAP_TOLERANCE_M = 0.5`), matching
   `Settings.boundary_gap_tolerance_meters` in `backend.core.config`.
2. Two parcels within `tolerance_m` in projected metric space (`HarmonizedParcel.geometry_projected`, SRID 32643)
   are evaluated via morphological closing (`ST_Buffer(ST_Buffer(ST_Union(a, b), tol/2), -tol/2) - ST_Union(a, b)`)
   to extract any inter-boundary polygonal void.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geometry
from shapely import wkt as shapely_wkt
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from sqlalchemy import Float, Select, and_, func, literal_column, select
from sqlalchemy.orm import aliased

from backend.models.parcel import HarmonizedParcel
from backend.topology.intersection import PROJECTED_SRID

DEFAULT_BOUNDARY_GAP_TOLERANCE_M: float = 0.5
_POLYGON_DIMENSION: int = 3
_MITRE_JOIN_STYLE: int = 2
_CLOSING_EPSILON: float = 1e-6
_MIN_POLYGON_PART_AREA_SQM: float = 1e-7


@dataclass(frozen=True, slots=True)
class BoundaryGapCandidate:
    """Detected inter-parcel boundary gap within the configured metric tolerance."""

    parcel_a_id: uuid.UUID | None
    parcel_b_id: uuid.UUID | None
    min_distance_m: float
    gap_area_sqm: float
    gap_geometry: MultiPolygon
    tolerance_m: float


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _validate_tolerance(tolerance_m: float) -> None:
    if not math.isfinite(tolerance_m) or tolerance_m <= 0.0:
        raise ValueError("tolerance_m must be > 0 and finite")


def _validate_min_gap_area(min_gap_area_sqm: float) -> None:
    if not math.isfinite(min_gap_area_sqm) or min_gap_area_sqm < 0.0:
        raise ValueError("min_gap_area_sqm must be >= 0 and finite")


def _normalize_to_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    if geom.is_empty:
        return MultiPolygon()
    if isinstance(geom, Polygon):
        return MultiPolygon([geom]) if geom.area > _MIN_POLYGON_PART_AREA_SQM else MultiPolygon()
    if isinstance(geom, MultiPolygon):
        parts = [p for p in geom.geoms if not p.is_empty and p.area > _MIN_POLYGON_PART_AREA_SQM]
        return MultiPolygon(parts)
    if isinstance(geom, GeometryCollection):
        polygons: list[Polygon] = []
        for part in geom.geoms:
            if isinstance(part, Polygon) and not part.is_empty and part.area > _MIN_POLYGON_PART_AREA_SQM:
                polygons.append(part)
            elif isinstance(part, MultiPolygon) and not part.is_empty:
                polygons.extend(
                    p for p in part.geoms if not p.is_empty and p.area > _MIN_POLYGON_PART_AREA_SQM
                )
        return MultiPolygon(polygons)
    return MultiPolygon()


def extract_pairwise_gap_geometry(
    geom_a: BaseGeometry | str,
    geom_b: BaseGeometry | str,
    tolerance_m: float = DEFAULT_BOUNDARY_GAP_TOLERANCE_M,
) -> MultiPolygon:
    """Extract the polygonal boundary gap bridged between `geom_a` and `geom_b` within `tolerance_m`."""
    _validate_tolerance(tolerance_m)
    poly_a = _coerce_geometry(geom_a)
    poly_b = _coerce_geometry(geom_b)

    if poly_a.is_empty or poly_b.is_empty:
        return MultiPolygon()

    min_dist = float(poly_a.distance(poly_b))
    if min_dist > tolerance_m:
        return MultiPolygon()

    union_ab = unary_union([poly_a, poly_b])
    closing_radius = (float(tolerance_m) / 2.0) + _CLOSING_EPSILON
    closed = union_ab.buffer(
        closing_radius,
        join_style=_MITRE_JOIN_STYLE,
    ).buffer(
        -closing_radius,
        join_style=_MITRE_JOIN_STYLE,
    )
    raw_gap = closed.difference(union_ab)
    return _normalize_to_multipolygon(raw_gap)


def evaluate_pairwise_boundary_gap(
    geom_a: BaseGeometry | str,
    geom_b: BaseGeometry | str,
    parcel_a_id: uuid.UUID | None = None,
    parcel_b_id: uuid.UUID | None = None,
    tolerance_m: float = DEFAULT_BOUNDARY_GAP_TOLERANCE_M,
    min_gap_area_sqm: float = 0.0,
) -> BoundaryGapCandidate | None:
    """Evaluate whether two projected parcel geometries form a boundary gap within `tolerance_m`."""
    _validate_tolerance(tolerance_m)
    _validate_min_gap_area(min_gap_area_sqm)

    poly_a = _coerce_geometry(geom_a)
    poly_b = _coerce_geometry(geom_b)
    if poly_a.is_empty or poly_b.is_empty:
        return None

    min_dist = float(poly_a.distance(poly_b))
    if min_dist > tolerance_m:
        return None

    gap_geom = extract_pairwise_gap_geometry(poly_a, poly_b, tolerance_m=tolerance_m)
    gap_area = float(gap_geom.area)
    if gap_geom.is_empty or gap_area <= min_gap_area_sqm:
        return None

    return BoundaryGapCandidate(
        parcel_a_id=parcel_a_id,
        parcel_b_id=parcel_b_id,
        min_distance_m=min_dist,
        gap_area_sqm=gap_area,
        gap_geometry=gap_geom,
        tolerance_m=tolerance_m,
    )


def detect_layer_boundary_gaps(
    parcels: Sequence[tuple[uuid.UUID, BaseGeometry | str]],
    tolerance_m: float = DEFAULT_BOUNDARY_GAP_TOLERANCE_M,
    min_gap_area_sqm: float = 0.0,
) -> list[BoundaryGapCandidate]:
    """Detect all pairwise boundary gaps in a layer, deduplicated and sorted by `(parcel_a_id, parcel_b_id)`."""
    _validate_tolerance(tolerance_m)
    _validate_min_gap_area(min_gap_area_sqm)

    normalized: list[tuple[uuid.UUID, BaseGeometry]] = sorted(
        ((pid, _coerce_geometry(geom)) for pid, geom in parcels),
        key=lambda item: item[0],
    )
    results: list[BoundaryGapCandidate] = []
    n = len(normalized)
    for i in range(n):
        id_a, geom_a = normalized[i]
        for j in range(i + 1, n):
            id_b, geom_b = normalized[j]
            if id_a >= id_b:
                continue
            candidate = evaluate_pairwise_boundary_gap(
                geom_a=geom_a,
                geom_b=geom_b,
                parcel_a_id=id_a,
                parcel_b_id=id_b,
                tolerance_m=tolerance_m,
                min_gap_area_sqm=min_gap_area_sqm,
            )
            if candidate is not None:
                results.append(candidate)
    return results


def pairwise_gap_geometry_expr(
    geom_a: Any,
    geom_b: Any,
    tolerance_m: float = DEFAULT_BOUNDARY_GAP_TOLERANCE_M,
) -> Any:
    """Build the PostGIS expression extracting the morphological-closing gap between `geom_a` and `geom_b`."""
    _validate_tolerance(tolerance_m)
    half_tol = float(tolerance_m) / 2.0
    union_expr = func.ST_Union(geom_a, geom_b)
    closed_expr = func.ST_Buffer(
        func.ST_Buffer(union_expr, half_tol),
        -half_tol,
    )
    diff_expr = func.ST_Difference(closed_expr, union_expr)
    return func.ST_Multi(
        func.ST_CollectionExtract(
            diff_expr,
            literal_column(str(_POLYGON_DIMENSION)),
        ),
        type_=Geometry(geometry_type="MULTIPOLYGON", srid=PROJECTED_SRID),
    )


def build_boundary_gap_detection_query(
    layer_id: uuid.UUID,
    tolerance_m: float = DEFAULT_BOUNDARY_GAP_TOLERANCE_M,
    min_gap_area_sqm: float = 0.0,
) -> Select:
    """Build the PostGIS query detecting pairwise boundary gaps within `tolerance_m` for `layer_id`."""
    _validate_tolerance(tolerance_m)
    _validate_min_gap_area(min_gap_area_sqm)

    a = aliased(HarmonizedParcel, name="a")
    b = aliased(HarmonizedParcel, name="b")

    gap_geom = pairwise_gap_geometry_expr(
        a.geometry_projected,
        b.geometry_projected,
        tolerance_m=tolerance_m,
    )
    gap_area = func.ST_Area(gap_geom, type_=Float)
    min_dist = func.ST_Distance(a.geometry_projected, b.geometry_projected, type_=Float)

    return (
        select(
            a.id.label("parcel_a_id"),
            b.id.label("parcel_b_id"),
            min_dist.label("min_distance_m"),
            gap_area.label("gap_area_sqm"),
            gap_geom.label("gap_geometry"),
        )
        .select_from(a)
        .join(
            b,
            and_(
                a.id < b.id,
                func.ST_DWithin(a.geometry_projected, b.geometry_projected, tolerance_m),
            ),
        )
        .where(
            a.layer_id == layer_id,
            b.layer_id == layer_id,
            gap_area > min_gap_area_sqm,
        )
        .order_by(a.id, b.id)
    )
