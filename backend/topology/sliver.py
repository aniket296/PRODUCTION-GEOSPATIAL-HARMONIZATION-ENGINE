"""Metric sliver detection using combined area and Polsby-Popper compactness thresholds.

Implements directive Section 16 sliver identification rules::

    0 < area_sqm < max_area_sqm (default 10.0 m²)
    AND
    0 < polsby_popper < max_polsby_popper (default 0.05)

All metric operations execute on ``HarmonizedParcel.geometry_projected`` (SRID 32643).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from shapely.geometry.base import BaseGeometry
from sqlalchemy import Float, Select, and_, func, literal_column, select

from backend.models.parcel import HarmonizedParcel
from backend.topology.compactness import evaluate_geometry_compactness, polsby_popper_expr

DEFAULT_SLIVER_MAX_AREA_SQM: float = 10.0
DEFAULT_SLIVER_MAX_POLSBY_POPPER: float = 0.05


@dataclass(frozen=True, slots=True)
class SliverEvaluationResult:
    """Metric evaluation and sliver classification result for a single parcel geometry."""

    parcel_id: uuid.UUID | None
    area_sqm: float
    perimeter_m: float
    polsby_popper: float
    is_sliver: bool
    max_area_sqm: float
    max_polsby_popper: float


def _validate_thresholds(max_area_sqm: float, max_polsby_popper: float) -> None:
    if not math.isfinite(max_area_sqm) or max_area_sqm <= 0.0:
        raise ValueError("max_area_sqm must be > 0 and finite")
    if not math.isfinite(max_polsby_popper) or max_polsby_popper <= 0.0 or max_polsby_popper > 1.0:
        raise ValueError("max_polsby_popper must be in (0.0, 1.0]")


def is_sliver_by_metrics(
    area_sqm: float,
    polsby_popper: float,
    max_area_sqm: float = DEFAULT_SLIVER_MAX_AREA_SQM,
    max_polsby_popper: float = DEFAULT_SLIVER_MAX_POLSBY_POPPER,
) -> bool:
    """Return `True` iff `0 < area_sqm < max_area_sqm` and `0 < polsby_popper < max_polsby_popper`."""
    _validate_thresholds(max_area_sqm, max_polsby_popper)
    if not math.isfinite(area_sqm) or area_sqm < 0.0:
        raise ValueError("area_sqm must be >= 0 and finite")
    if not math.isfinite(polsby_popper) or polsby_popper < 0.0 or polsby_popper > 1.0:
        raise ValueError("polsby_popper must be in [0.0, 1.0]")

    return bool(0.0 < area_sqm < max_area_sqm and 0.0 < polsby_popper < max_polsby_popper)


def evaluate_geometry_sliver(
    geom: BaseGeometry | str,
    parcel_id: uuid.UUID | None = None,
    max_area_sqm: float = DEFAULT_SLIVER_MAX_AREA_SQM,
    max_polsby_popper: float = DEFAULT_SLIVER_MAX_POLSBY_POPPER,
) -> SliverEvaluationResult:
    """Compute metric area, perimeter, Polsby-Popper compactness, and sliver status for `geom`."""
    _validate_thresholds(max_area_sqm, max_polsby_popper)
    metrics = evaluate_geometry_compactness(geom)
    sliver_flag = is_sliver_by_metrics(
        area_sqm=metrics.area_sqm,
        polsby_popper=metrics.polsby_popper,
        max_area_sqm=max_area_sqm,
        max_polsby_popper=max_polsby_popper,
    )
    return SliverEvaluationResult(
        parcel_id=parcel_id,
        area_sqm=metrics.area_sqm,
        perimeter_m=metrics.perimeter_m,
        polsby_popper=metrics.polsby_popper,
        is_sliver=sliver_flag,
        max_area_sqm=max_area_sqm,
        max_polsby_popper=max_polsby_popper,
    )


def detect_layer_slivers(
    parcels: Sequence[tuple[uuid.UUID, BaseGeometry | str]],
    max_area_sqm: float = DEFAULT_SLIVER_MAX_AREA_SQM,
    max_polsby_popper: float = DEFAULT_SLIVER_MAX_POLSBY_POPPER,
) -> list[SliverEvaluationResult]:
    """Return all sliver parcels from `parcels` sorted deterministically by `parcel_id`."""
    _validate_thresholds(max_area_sqm, max_polsby_popper)
    results: list[SliverEvaluationResult] = []
    for parcel_id, geom in sorted(parcels, key=lambda item: item[0]):
        evaluated = evaluate_geometry_sliver(
            geom=geom,
            parcel_id=parcel_id,
            max_area_sqm=max_area_sqm,
            max_polsby_popper=max_polsby_popper,
        )
        if evaluated.is_sliver:
            results.append(evaluated)
    return results


def sliver_predicate_expr(
    geom: Any,
    *,
    max_area_sqm: float = DEFAULT_SLIVER_MAX_AREA_SQM,
    max_polsby_popper: float = DEFAULT_SLIVER_MAX_POLSBY_POPPER,
) -> Any:
    """Build the SQLAlchemy boolean predicate `0 < ST_Area(geom) < max_area_sqm AND 0 < PP(geom) < max_pp`."""
    _validate_thresholds(max_area_sqm, max_polsby_popper)
    zero = literal_column("0.0", type_=Float)
    area_expr = func.ST_Area(geom, type_=Float)
    pp_expr = polsby_popper_expr(geom)
    return and_(
        area_expr > zero,
        area_expr < max_area_sqm,
        pp_expr > zero,
        pp_expr < max_polsby_popper,
    )


def build_sliver_detection_query(
    layer_id: uuid.UUID,
    max_area_sqm: float = DEFAULT_SLIVER_MAX_AREA_SQM,
    max_polsby_popper: float = DEFAULT_SLIVER_MAX_POLSBY_POPPER,
) -> Select:
    """Build the PostGIS query listing every sliver parcel in `layer_id` in deterministic `id` order."""
    _validate_thresholds(max_area_sqm, max_polsby_popper)

    geom = HarmonizedParcel.geometry_projected
    area_expr = func.ST_Area(geom, type_=Float)
    perim_expr = func.ST_Perimeter(geom, type_=Float)
    pp_expr = polsby_popper_expr(geom)

    return (
        select(
            HarmonizedParcel.id.label("parcel_id"),
            HarmonizedParcel.parcel_identifier.label("parcel_identifier"),
            area_expr.label("area_sqm"),
            perim_expr.label("perimeter_m"),
            pp_expr.label("polsby_popper"),
            geom.label("geometry_projected"),
        )
        .where(
            HarmonizedParcel.layer_id == layer_id,
            sliver_predicate_expr(
                geom,
                max_area_sqm=max_area_sqm,
                max_polsby_popper=max_polsby_popper,
            ),
        )
        .order_by(HarmonizedParcel.id)
    )
