"""Deterministic metric grid normalization via ST_SnapToGrid / coordinate rounding (Step 99/150).

Directive Sections 19, 20-23, 25:
- Eliminates floating-point micro-drift on projected metric geometries (`EPSG:32643`) by snapping all coordinates to a
  uniform metric grid (`Settings.grid_tolerance_meters`, default `0.001` m = 1 millimeter).
- Dispute-locked parcels (`dispute_locked=True`) are never modified (`skipped_locked_ids`).
- Sub-grid degenerate polygons that collapse or become invalid when snapped to the grid raise `GridSnapValidityError`
  (a `ValueError`) and are recorded in `rejected_parcel_ids` during batch execution.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geometry
from geoalchemy2.shape import from_shape, to_shape
from shapely import wkt as shapely_wkt
from shapely.geometry import GeometryCollection, LinearRing, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity
from sqlalchemy import func, literal_column

from backend.core.config import get_settings
from backend.healing.boundary_snap import measure_max_displacement_m
from backend.models.parcel import HarmonizedParcel
from backend.topology.intersection import PROJECTED_SRID
from backend.topology.recorder import project_metric_geometry_to_wgs84

_POLYGON_DIMENSION = 3
_DISPLACEMENT_EPSILON_M = 1e-9


class GridSnapValidityError(ValueError):
    """Raised when snapping a geometry to the metric grid collapses it or produces an invalid polygon."""


@dataclass(frozen=True, slots=True)
class GridSnapCandidateInput:
    """A parcel candidate considered for batch metric grid normalization."""

    parcel_id: uuid.UUID
    geometry: BaseGeometry | str
    dispute_locked: bool = False


@dataclass(frozen=True, slots=True)
class GridSnapResult:
    """Outcome of snapping a single parcel geometry to the metric coordinate grid."""

    parcel_id: uuid.UUID | None
    geometry_after: MultiPolygon
    max_displacement_m: float
    area_before_sqm: float
    area_after_sqm: float
    normalized: bool


@dataclass(frozen=True, slots=True)
class GridSnapBatchResult:
    """Outcome of deterministic batch metric grid normalization."""

    results: tuple[GridSnapResult, ...]
    updated_geometries: dict[uuid.UUID, MultiPolygon]
    skipped_locked_ids: tuple[uuid.UUID, ...]
    rejected_parcel_ids: tuple[uuid.UUID, ...]


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _resolve_grid_size(grid_size_m: float | None) -> float:
    size = get_settings().grid_tolerance_meters if grid_size_m is None else float(grid_size_m)
    if not math.isfinite(size) or size <= 0.0:
        raise ValueError("grid_size_m must be > 0 and finite")
    return size


def _polygons_of(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        parts: list[Polygon] = []
        for child in geom.geoms:
            parts.extend(_polygons_of(child))
        return parts
    return []


def _as_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    return geom if isinstance(geom, MultiPolygon) else MultiPolygon(_polygons_of(geom))


def _snap_scalar(val: float, grid_size_m: float, decimals: int) -> float:
    snapped = round(val / grid_size_m) * grid_size_m
    return round(snapped, decimals)


def _snap_ring_coords(
    coords: Sequence[tuple[float, ...]],
    grid_size_m: float,
    decimals: int,
) -> tuple[list[tuple[float, float]], bool]:
    """Snap ring coordinates to `grid_size_m` and deduplicate consecutive identical vertices."""
    snapped_pts: list[tuple[float, float]] = []
    changed = False
    for pt in coords:
        x0, y0 = float(pt[0]), float(pt[1])
        x1 = _snap_scalar(x0, grid_size_m, decimals)
        y1 = _snap_scalar(y0, grid_size_m, decimals)
        if x1 != x0 or y1 != y0:
            changed = True
        if not snapped_pts or snapped_pts[-1] != (x1, y1):
            snapped_pts.append((x1, y1))

    if snapped_pts and snapped_pts[0] != snapped_pts[-1]:
        snapped_pts.append(snapped_pts[0])
    if len(snapped_pts) != len(coords):
        changed = True
    return snapped_pts, changed


def snap_geometry_to_grid(
    geometry: BaseGeometry | str,
    *,
    grid_size_m: float | None = None,
    parcel_id: uuid.UUID | None = None,
) -> GridSnapResult:
    """Snap all vertices of `geometry` to the nearest `grid_size_m` step in projected metric coordinates.

    Raises:
        ValueError: If `grid_size_m` is not > 0 and finite.
        GridSnapValidityError: If snapping collapses the geometry or produces an invalid polygon.
    """
    size = _resolve_grid_size(grid_size_m)
    decimals = max(0, int(math.ceil(-math.log10(size))) + 6)
    original = _coerce_geometry(geometry)
    before_mp = _as_multipolygon(original)
    area_before = float(before_mp.area)
    label = str(parcel_id) if parcel_id is not None else "<unnamed>"

    if before_mp.is_empty or area_before <= 0.0:
        raise GridSnapValidityError(f"Parcel {label} has empty or zero-area input geometry")

    snapped_polygons: list[Polygon] = []
    any_changed = False

    for poly in before_mp.geoms:
        ext_pts, ext_changed = _snap_ring_coords(list(poly.exterior.coords), size, decimals)
        any_changed = any_changed or ext_changed
        if len(ext_pts) < 4:
            raise GridSnapValidityError(
                f"Grid normalization of parcel {label} collapsed exterior ring to < 3 distinct vertices"
            )

        holes: list[LinearRing] = []
        for interior in poly.interiors:
            hole_pts, hole_changed = _snap_ring_coords(list(interior.coords), size, decimals)
            any_changed = any_changed or hole_changed
            if len(hole_pts) >= 4:
                hole_ring = LinearRing(hole_pts)
                if hole_ring.is_valid and Polygon(hole_ring).area > 0.0:
                    holes.append(hole_ring)

        snapped_poly = Polygon(ext_pts, holes)
        if not snapped_poly.is_Empty if hasattr(snapped_poly, "is_Empty") else not snapped_poly.is_empty:
            snapped_polygons.append(snapped_poly)

    if not snapped_polygons:
        raise GridSnapValidityError(f"Grid normalization of parcel {label} collapsed to empty geometry")

    after_mp = MultiPolygon(snapped_polygons)
    if after_mp.is_empty or after_mp.area <= 0.0:
        raise GridSnapValidityError(f"Grid normalization of parcel {label} collapsed to zero area")
    if not after_mp.is_valid:
        raise GridSnapValidityError(
            f"Grid normalization of parcel {label} produced invalid geometry: {explain_validity(after_mp)}"
        )

    if not any_changed:
        return GridSnapResult(
            parcel_id=parcel_id,
            geometry_after=before_mp,
            max_displacement_m=0.0,
            area_before_sqm=area_before,
            area_after_sqm=area_before,
            normalized=False,
        )

    displacement = measure_max_displacement_m(before_mp, after_mp)
    max_allowed = math.hypot(0.5 * size, 0.5 * size) + _DISPLACEMENT_EPSILON_M
    if displacement > max_allowed:
        raise GridSnapValidityError(
            f"Grid normalization of parcel {label} is invalid: displacement {displacement:.6f} m "
            f"exceeds grid half-diagonal {max_allowed:.6f} m"
        )

    return GridSnapResult(
        parcel_id=parcel_id,
        geometry_after=after_mp,
        max_displacement_m=displacement,
        area_before_sqm=area_before,
        area_after_sqm=float(after_mp.area),
        normalized=True,
    )


def normalize_parcel_grid(
    candidates: Sequence[GridSnapCandidateInput],
    *,
    grid_size_m: float | None = None,
) -> GridSnapBatchResult:
    """Deterministically normalize unlocked candidate geometries to the metric coordinate grid."""
    size = _resolve_grid_size(grid_size_m)
    ordered = sorted(candidates, key=lambda c: c.parcel_id)

    results: list[GridSnapResult] = []
    updated: dict[uuid.UUID, MultiPolygon] = {}
    skipped_locked: list[uuid.UUID] = []
    rejected: list[uuid.UUID] = []

    for cand in ordered:
        if cand.dispute_locked:
            skipped_locked.append(cand.parcel_id)
            continue
        try:
            res = snap_geometry_to_grid(cand.geometry, grid_size_m=size, parcel_id=cand.parcel_id)
        except GridSnapValidityError:
            rejected.append(cand.parcel_id)
            continue

        results.append(res)
        if res.normalized:
            updated[cand.parcel_id] = res.geometry_after

    return GridSnapBatchResult(
        results=tuple(results),
        updated_geometries=updated,
        skipped_locked_ids=tuple(skipped_locked),
        rejected_parcel_ids=tuple(rejected),
    )


def normalize_layer_grid(
    parcels: Sequence[HarmonizedParcel],
    *,
    grid_size_m: float | None = None,
) -> GridSnapBatchResult:
    """Apply deterministic metric grid normalization in-place on `HarmonizedParcel` ORM records."""
    by_id = {p.id: p for p in parcels}
    candidates = [
        GridSnapCandidateInput(
            parcel_id=p.id,
            geometry=to_shape(p.geometry_projected),
            dispute_locked=bool(p.dispute_locked),
        )
        for p in parcels
    ]
    result = normalize_parcel_grid(candidates, grid_size_m=grid_size_m)

    for pid, mp in result.updated_geometries.items():
        parcel = by_id[pid]
        parcel.geometry_projected = from_shape(mp, srid=PROJECTED_SRID)
        parcel.geometry_wgs84 = from_shape(project_metric_geometry_to_wgs84(mp), srid=4326)
        parcel.recalculate_metrics(mp)
        parcel.mark_auto_resolved()

    return result


def grid_snap_geometry_expr(geom: Any, grid_size_m: float = 0.001) -> Any:
    """PostGIS expression snapping `geom` to a uniform metric coordinate grid.

    Compiles to ``ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SnapToGrid(geom, grid_size_m)), 3))``.
    """
    size = _resolve_grid_size(grid_size_m)
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_MakeValid(func.ST_SnapToGrid(geom, size)),
            literal_column(str(_POLYGON_DIMENSION)),
        ),
        type_=Geometry(geometry_type="MULTIPOLYGON", srid=PROJECTED_SRID),
    )
