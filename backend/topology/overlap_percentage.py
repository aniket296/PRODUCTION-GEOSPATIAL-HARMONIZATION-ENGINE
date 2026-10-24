"""Deterministic pairwise overlap percentage calculations and PostGIS query builders.

Implements directive Section 15 formulas::

    intersection_area = area(A ∩ B)
    overlap_pct_a = (intersection_area / area_a) * 100
    overlap_pct_b = (intersection_area / area_b) * 100

Both percentages are always evaluated independently so that an encroachment into a small parcel is never
masked by the large denominator of an adjacent estate. All geometric operations execute in the metric
projected CRS (`HarmonizedParcel.geometry_projected`, SRID 32643).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPolygon
from shapely.geometry.base import BaseGeometry
from sqlalchemy import Float, Select, and_, case, cast, func, literal_column, select
from sqlalchemy.orm import aliased

from backend.models.parcel import HarmonizedParcel
from backend.topology.intersection import (
    extract_polygonal_intersection,
    intersection_area_expr,
    intersection_geometry_expr,
)


@dataclass(frozen=True, slots=True)
class OverlapPercentageMetrics:
    """Independent directional overlap metrics for a pair of cadastral parcels."""

    parcel_a_id: uuid.UUID | None
    parcel_b_id: uuid.UUID | None
    area_a_sqm: float
    area_b_sqm: float
    intersection_area_sqm: float
    overlap_percentage_primary: float
    overlap_percentage_secondary: float
    max_overlap_percentage: float
    min_overlap_percentage: float
    intersection_geometry: MultiPolygon


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def compute_overlap_percentage(
    intersection_area_sqm: float,
    parcel_area_sqm: float,
) -> float:
    """Compute `(intersection_area_sqm / parcel_area_sqm) * 100.0` clamped to `[0.0, 100.0]`.

    Raises:
        ValueError: If `parcel_area_sqm <= 0` or `intersection_area_sqm < 0` or either value is NaN/inf.
    """
    if not math.isfinite(parcel_area_sqm) or parcel_area_sqm <= 0.0:
        raise ValueError("parcel_area_sqm must be > 0 and finite")
    if not math.isfinite(intersection_area_sqm) or intersection_area_sqm < 0.0:
        raise ValueError("intersection_area_sqm must be >= 0 and finite")
    if intersection_area_sqm == 0.0:
        return 0.0

    raw_pct = (float(intersection_area_sqm) / float(parcel_area_sqm)) * 100.0
    return min(100.0, max(0.0, raw_pct))


def compute_overlap_percentages(
    intersection_area_sqm: float,
    area_a_sqm: float,
    area_b_sqm: float,
) -> tuple[float, float]:
    """Compute `(overlap_pct_a, overlap_pct_b)` independently against both parcel areas."""
    pct_a = compute_overlap_percentage(intersection_area_sqm=intersection_area_sqm, parcel_area_sqm=area_a_sqm)
    pct_b = compute_overlap_percentage(intersection_area_sqm=intersection_area_sqm, parcel_area_sqm=area_b_sqm)
    return pct_a, pct_b


def evaluate_pair_overlap_metrics(
    geom_a: BaseGeometry | str,
    geom_b: BaseGeometry | str,
    parcel_a_id: uuid.UUID | None = None,
    parcel_b_id: uuid.UUID | None = None,
) -> OverlapPercentageMetrics:
    """Evaluate intersection geometry, metric areas, and independent overlap percentages for two geometries."""
    poly_a = _coerce_geometry(geom_a)
    poly_b = _coerce_geometry(geom_b)

    area_a_sqm = float(poly_a.area)
    area_b_sqm = float(poly_b.area)
    if area_a_sqm <= 0.0 or area_b_sqm <= 0.0:
        raise ValueError("Both geometries must have strictly positive metric area (> 0 m²)")

    inter_geom = extract_polygonal_intersection(poly_a, poly_b)
    inter_area_sqm = float(inter_geom.area)
    pct_a, pct_b = compute_overlap_percentages(
        intersection_area_sqm=inter_area_sqm,
        area_a_sqm=area_a_sqm,
        area_b_sqm=area_b_sqm,
    )

    return OverlapPercentageMetrics(
        parcel_a_id=parcel_a_id,
        parcel_b_id=parcel_b_id,
        area_a_sqm=area_a_sqm,
        area_b_sqm=area_b_sqm,
        intersection_area_sqm=inter_area_sqm,
        overlap_percentage_primary=pct_a,
        overlap_percentage_secondary=pct_b,
        max_overlap_percentage=max(pct_a, pct_b),
        min_overlap_percentage=min(pct_a, pct_b),
        intersection_geometry=inter_geom,
    )


def overlap_percentage_expr(intersection_area: Any, parcel_area: Any) -> Any:
    """Build a division-safe SQL expression computing `LEAST(100.0, (intersection_area / NULLIF(parcel_area, 0.0)) * 100.0)`."""
    zero = literal_column("0.0", type_=Float)
    hundred = literal_column("100.0", type_=Float)
    safe_denom = func.NULLIF(cast(parcel_area, Float), zero)
    raw_pct = (cast(intersection_area, Float) / safe_denom) * hundred
    return cast(
        case(
            (cast(parcel_area, Float) <= zero, zero),
            else_=func.LEAST(hundred, func.GREATEST(zero, raw_pct)),
        ),
        Float,
    )


def build_pairwise_overlap_percentage_query(
    layer_id: uuid.UUID,
    min_intersection_area_sqm: float = 0.0,
) -> Select:
    """Build the PostGIS query computing independent overlap percentages for every overlapping pair in `layer_id`."""
    if min_intersection_area_sqm < 0.0:
        raise ValueError("min_intersection_area_sqm must be >= 0")

    a = aliased(HarmonizedParcel, name="a")
    b = aliased(HarmonizedParcel, name="b")

    area_a = func.ST_Area(a.geometry_projected, type_=Float)
    area_b = func.ST_Area(b.geometry_projected, type_=Float)
    inter_area = intersection_area_expr(a.geometry_projected, b.geometry_projected)
    inter_geom = intersection_geometry_expr(a.geometry_projected, b.geometry_projected)

    return (
        select(
            a.id.label("parcel_a_id"),
            b.id.label("parcel_b_id"),
            area_a.label("area_a_sqm"),
            area_b.label("area_b_sqm"),
            inter_area.label("intersection_area_sqm"),
            overlap_percentage_expr(inter_area, area_a).label("overlap_percentage_primary"),
            overlap_percentage_expr(inter_area, area_b).label("overlap_percentage_secondary"),
            inter_geom.label("intersection_geometry"),
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
            inter_area > min_intersection_area_sqm,
        )
        .order_by(a.id, b.id)
    )
