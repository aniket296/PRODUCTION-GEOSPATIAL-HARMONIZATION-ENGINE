"""Deterministic metric boundary snapping with displacement and validity assertions (Step 97/150).

Directive Sections 18, 20-23, 25:
- Sub-tolerance boundary gaps between adjacent parcels are closed by snapping parcel vertices onto the vertices and
  segments of reference parcels (`shapely.ops.snap` in Python, `ST_Snap` in PostGIS), always in projected metric
  coordinates (EPSG:32643).
- Default tolerance is `Settings.snap_healing_tolerance_meters` (0.25 m).
- Displacement assertion: the Hausdorff distance between original and snapped geometry never exceeds the tolerance.
- Validity assertion: snapped output must be a valid, non-empty polygonal geometry with positive area. Invalid output
  is never silently repaired; `SnapValidityError` is raised instead.
- Batch determinism: parcels are processed in ascending UUID order. Each unlocked parcel snaps onto the current
  geometry of every lower-UUID parcel plus every dispute-locked parcel. Dispute-locked parcels are fixed anchors and
  are never modified.
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
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import snap
from shapely.validation import explain_validity
from sqlalchemy import func, literal_column

from backend.core.config import get_settings
from backend.models.parcel import HarmonizedParcel
from backend.topology.intersection import PROJECTED_SRID
from backend.topology.recorder import project_metric_geometry_to_wgs84

#: ST_CollectionExtract type code selecting polygonal geometries.
_POLYGON_DIMENSION = 3
#: Floating-point slack when comparing displacement against the tolerance.
_DISPLACEMENT_EPSILON_M = 1e-9


class SnapValidityError(ValueError):
    """Raised when snapping yields invalid, empty, or over-displaced geometry."""


@dataclass(frozen=True, slots=True)
class SnapCandidateInput:
    """A parcel considered for batch boundary snapping (projected metric geometry)."""

    parcel_id: uuid.UUID
    geometry: BaseGeometry | str
    dispute_locked: bool = False


@dataclass(frozen=True, slots=True)
class BoundarySnapResult:
    """Outcome of snapping a single parcel onto its reference geometry."""

    parcel_id: uuid.UUID | None
    geometry_after: MultiPolygon
    max_displacement_m: float
    area_before_sqm: float
    area_after_sqm: float
    snapped: bool


@dataclass(frozen=True, slots=True)
class BoundarySnapBatchResult:
    """Outcome of deterministic batch boundary snapping."""

    results: tuple[BoundarySnapResult, ...]
    updated_geometries: dict[uuid.UUID, MultiPolygon]
    skipped_locked_ids: tuple[uuid.UUID, ...]
    rejected_parcel_ids: tuple[uuid.UUID, ...]


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _resolve_tolerance(tolerance_m: float | None) -> float:
    tol = get_settings().snap_healing_tolerance_meters if tolerance_m is None else float(tolerance_m)
    if not math.isfinite(tol) or tol <= 0.0:
        raise ValueError("tolerance_m must be > 0 and finite")
    return tol


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


def measure_max_displacement_m(before: BaseGeometry, after: BaseGeometry) -> float:
    """Return the maximum metric displacement between two geometries (Hausdorff distance)."""
    return float(before.hausdorff_distance(after))


def snap_geometry_to_reference(
    geometry: BaseGeometry | str,
    reference: BaseGeometry | str,
    *,
    tolerance_m: float | None = None,
    parcel_id: uuid.UUID | None = None,
) -> BoundarySnapResult:
    """Snap `geometry` onto the vertices/segments of `reference` within `tolerance_m`.

    Raises:
        ValueError: If `tolerance_m` is not > 0 and finite.
        SnapValidityError: If the snapped geometry is invalid, empty, non-polygonal, or displaced beyond tolerance.
    """
    tol = _resolve_tolerance(tolerance_m)
    original = _coerce_geometry(geometry)
    ref = _coerce_geometry(reference)
    before_mp = _as_multipolygon(original)
    area_before = float(before_mp.area)
    label = str(parcel_id) if parcel_id is not None else "<unnamed>"

    snapped_geom = snap(original, ref, tol)

    if snapped_geom.is_empty:
        raise SnapValidityError(f"Snapping parcel {label} produced empty (invalid) geometry")
    if not snapped_geom.is_valid:
        raise SnapValidityError(
            f"Snapping parcel {label} produced invalid geometry: {explain_validity(snapped_geom)}"
        )
    after_mp = _as_multipolygon(snapped_geom)
    if after_mp.is_empty or after_mp.area <= 0.0 or not after_mp.is_valid:
        raise SnapValidityError(f"Snapping parcel {label} collapsed to invalid non-polygonal geometry")

    if after_mp.equals(before_mp):
        return BoundarySnapResult(
            parcel_id=parcel_id,
            geometry_after=before_mp,
            max_displacement_m=0.0,
            area_before_sqm=area_before,
            area_after_sqm=area_before,
            snapped=False,
        )

    displacement = measure_max_displacement_m(before_mp, after_mp)
    if displacement > tol + _DISPLACEMENT_EPSILON_M:
        raise SnapValidityError(
            f"Snapping parcel {label} is invalid: displacement {displacement:.6f} m exceeds tolerance {tol:.6f} m"
        )

    return BoundarySnapResult(
        parcel_id=parcel_id,
        geometry_after=after_mp,
        max_displacement_m=displacement,
        area_before_sqm=area_before,
        area_after_sqm=float(after_mp.area),
        snapped=True,
    )


def snap_parcel_boundaries(
    candidates: Sequence[SnapCandidateInput],
    *,
    tolerance_m: float | None = None,
) -> BoundarySnapBatchResult:
    """Deterministically snap unlocked parcels onto lower-UUID and dispute-locked anchors."""
    tol = _resolve_tolerance(tolerance_m)
    ordered = sorted(candidates, key=lambda c: c.parcel_id)
    current: dict[uuid.UUID, BaseGeometry] = {c.parcel_id: _coerce_geometry(c.geometry) for c in ordered}
    locked_ids = [c.parcel_id for c in ordered if c.dispute_locked]

    results: list[BoundarySnapResult] = []
    updated: dict[uuid.UUID, MultiPolygon] = {}
    rejected: list[uuid.UUID] = []

    for idx, cand in enumerate(ordered):
        pid = cand.parcel_id
        if cand.dispute_locked:
            continue

        ref_ids = [c.parcel_id for c in ordered[:idx] if not c.dispute_locked]
        ref_ids += [lid for lid in locked_ids if lid != pid]
        ref_parts: list[Polygon] = []
        for rid in sorted(set(ref_ids)):
            ref_parts.extend(_polygons_of(current[rid]))

        if not ref_parts:
            mp = _as_multipolygon(current[pid])
            results.append(
                BoundarySnapResult(
                    parcel_id=pid,
                    geometry_after=mp,
                    max_displacement_m=0.0,
                    area_before_sqm=float(mp.area),
                    area_after_sqm=float(mp.area),
                    snapped=False,
                )
            )
            continue

        try:
            res = snap_geometry_to_reference(
                current[pid], GeometryCollection(ref_parts), tolerance_m=tol, parcel_id=pid
            )
        except SnapValidityError:
            rejected.append(pid)
            continue

        results.append(res)
        if res.snapped:
            current[pid] = res.geometry_after
            updated[pid] = res.geometry_after

    return BoundarySnapBatchResult(
        results=tuple(results),
        updated_geometries=updated,
        skipped_locked_ids=tuple(locked_ids),
        rejected_parcel_ids=tuple(rejected),
    )


def snap_layer_boundaries(
    parcels: Sequence[HarmonizedParcel],
    *,
    tolerance_m: float | None = None,
) -> BoundarySnapBatchResult:
    """Apply deterministic boundary snapping in-place on `HarmonizedParcel` ORM records."""
    by_id = {p.id: p for p in parcels}
    candidates = [
        SnapCandidateInput(
            parcel_id=p.id,
            geometry=to_shape(p.geometry_projected),
            dispute_locked=bool(p.dispute_locked),
        )
        for p in parcels
    ]
    result = snap_parcel_boundaries(candidates, tolerance_m=tolerance_m)

    for pid, mp in result.updated_geometries.items():
        parcel = by_id[pid]
        parcel.geometry_projected = from_shape(mp, srid=PROJECTED_SRID)
        parcel.geometry_wgs84 = from_shape(project_metric_geometry_to_wgs84(mp), srid=4326)
        parcel.recalculate_metrics(mp)
        parcel.mark_auto_resolved()

    return result


def boundary_snap_geometry_expr(geom: Any, reference: Any, tolerance_m: float) -> Any:
    """PostGIS expression snapping `geom` onto `reference` within a metric tolerance.

    Compiles to ``ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Snap(geom, reference, tol)), 3))``.
    """
    tol = _resolve_tolerance(tolerance_m)
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_MakeValid(func.ST_Snap(geom, reference, tol)),
            literal_column(str(_POLYGON_DIMENSION)),
        ),
        type_=Geometry(geometry_type="MULTIPOLYGON", srid=PROJECTED_SRID),
    )
