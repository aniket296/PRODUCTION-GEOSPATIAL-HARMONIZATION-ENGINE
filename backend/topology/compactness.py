"""Polsby-Popper compactness helper, geometric evaluator, and PostGIS SQL expression builder.

Implements directive Section 16 mathematical definition::

    Polsby-Popper = (4 * pi * Area) / (Perimeter ** 2)

All geometric evaluations execute in the metric projected CRS (`HarmonizedParcel.geometry_projected`, SRID 32643).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry
from sqlalchemy import Float, case, cast, func, literal_column

FOUR_PI: float = 4.0 * math.pi


@dataclass(frozen=True, slots=True)
class CompactnessMetrics:
    """Metric area, perimeter, and Polsby-Popper compactness score for a projected geometry."""

    area_sqm: float
    perimeter_m: float
    polsby_popper: float


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def compute_polsby_popper(area_sqm: float, perimeter_m: float) -> float:
    """Compute Polsby-Popper compactness `(4 * pi * area_sqm) / (perimeter_m ** 2)` clamped to `[0.0, 1.0]`.

    Returns `0.0` when `area_sqm == 0.0` or `perimeter_m == 0.0`.

    Raises:
        ValueError: If `area_sqm` or `perimeter_m` is negative or non-finite (`NaN` / `inf`).
    """
    if not math.isfinite(area_sqm) or area_sqm < 0.0:
        raise ValueError("area_sqm must be >= 0 and finite")
    if not math.isfinite(perimeter_m) or perimeter_m < 0.0:
        raise ValueError("perimeter_m must be >= 0 and finite")
    if area_sqm == 0.0 or perimeter_m == 0.0:
        return 0.0

    raw_pp = (FOUR_PI * float(area_sqm)) / (float(perimeter_m) * float(perimeter_m))
    return min(1.0, max(0.0, raw_pp))


def evaluate_geometry_compactness(geom: BaseGeometry | str) -> CompactnessMetrics:
    """Evaluate metric area (`m²`), boundary perimeter (`m`), and Polsby-Popper compactness for `geom`."""
    poly = _coerce_geometry(geom)
    if poly.is_empty:
        return CompactnessMetrics(area_sqm=0.0, perimeter_m=0.0, polsby_popper=0.0)

    area_sqm = float(poly.area)
    perimeter_m = float(poly.length)
    if area_sqm <= 0.0 or perimeter_m <= 0.0:
        return CompactnessMetrics(area_sqm=0.0, perimeter_m=0.0, polsby_popper=0.0)

    pp = compute_polsby_popper(area_sqm=area_sqm, perimeter_m=perimeter_m)
    return CompactnessMetrics(
        area_sqm=area_sqm,
        perimeter_m=perimeter_m,
        polsby_popper=pp,
    )


def polsby_popper_expr(geom_or_area: Any, perimeter: Any | None = None) -> Any:
    """Build a division-safe SQLAlchemy Float expression computing Polsby-Popper compactness.

    Args:
        geom_or_area: Either a PostGIS geometry column/expression (when `perimeter is None`) or a numeric
            area column/expression (when `perimeter` is provided).
        perimeter: Optional numeric perimeter column/expression in meters.
    """
    if perimeter is None:
        area_expr = func.ST_Area(geom_or_area, type_=Float)
        perim_expr = func.ST_Perimeter(geom_or_area, type_=Float)
    else:
        area_expr = cast(geom_or_area, Float)
        perim_expr = cast(perimeter, Float)

    zero = literal_column("0.0", type_=Float)
    one = literal_column("1.0", type_=Float)
    four_pi = literal_column(repr(FOUR_PI), type_=Float)

    denom = func.NULLIF(perim_expr * perim_expr, zero)
    raw_pp = (four_pi * area_expr) / denom

    return cast(
        case(
            ((area_expr <= zero) | (perim_expr <= zero), zero),
            else_=func.LEAST(one, func.GREATEST(zero, raw_pp)),
        ),
        Float,
    )
