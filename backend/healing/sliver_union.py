"""Deterministic sliver assignment, polygon union, and neighbor metric recalculation (Step 91/150).

Directive Sections 16, 20-22, 25:
- Metric slivers (`area < 10 m²` AND `Polsby-Popper < 0.05`) that are not dispute-locked are assigned in ascending
  parcel UUID order to the eligible neighbor (non-sliver, not dispute-locked) sharing the longest linear boundary.
- Ties on shared boundary length resolve deterministically to the smallest neighbor parcel UUID.
- Neighbor geometries are updated in-place as slivers are merged, allowing multiple slivers to accumulate into a
  single neighbor while checking total area conservation within `area_tolerance_sqm`.
- After union, the receiving neighbor's metric area, perimeter, and Polsby-Popper compactness are recalculated in
  projected metric coordinates (`EPSG:32643`), and its WGS 84 geometry (`EPSG:4326`) is updated.
- Merged sliver records preserve their original geometry for auditability, record `merged_into_parcel_id` in
  `attributes`, and transition to `AUTO_RESOLVED`.
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
from shapely.ops import unary_union
from shapely.validation import make_valid
from sqlalchemy import func, literal_column

from backend.healing.shared_boundary import (
    SharedBoundaryCandidateInput,
    identify_longest_shared_boundary_neighbor,
)
from backend.models.parcel import HarmonizedParcel
from backend.topology.compactness import evaluate_geometry_compactness
from backend.topology.intersection import PROJECTED_SRID
from backend.topology.recorder import project_metric_geometry_to_wgs84

#: ST_CollectionExtract type code selecting polygonal geometries (1 = point, 2 = line, 3 = polygon).
_POLYGON_DIMENSION = 3


@dataclass(frozen=True, slots=True)
class NeighborMetrics:
    """Recalculated projected metric properties for a neighbor parcel after sliver union."""

    parcel_id: uuid.UUID
    area_sqm: float
    perimeter_m: float
    polsby_popper: float


@dataclass(frozen=True, slots=True)
class SliverAssignment:
    """Audit record of a sliver merged into an adjacent neighbor parcel."""

    sliver_parcel_id: uuid.UUID
    neighbor_parcel_id: uuid.UUID
    shared_length_m: float
    sliver_area_sqm: float
    neighbor_area_before_sqm: float
    neighbor_area_after_sqm: float


@dataclass(frozen=True, slots=True)
class SliverUnionResult:
    """Deterministic outcome of healing slivers across a set of candidate parcels."""

    assignments: tuple[SliverAssignment, ...]
    unassigned_sliver_ids: tuple[uuid.UUID, ...]
    updated_geometries: dict[uuid.UUID, MultiPolygon]
    neighbor_metrics: dict[uuid.UUID, NeighborMetrics]


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _to_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    """Normalize a polygonal geometry (after union/make_valid) into a valid MultiPolygon."""
    valid = geom if geom.is_valid else make_valid(geom)
    polygons: list[Polygon] = []

    def _collect(part: BaseGeometry) -> None:
        if part.is_empty:
            return
        if isinstance(part, Polygon):
            if part.area > 0.0:
                polygons.append(part)
        elif isinstance(part, (MultiPolygon, GeometryCollection)):
            for child in part.geoms:
                _collect(child)

    _collect(valid)
    mp = MultiPolygon(polygons)
    if not mp.is_valid:
        repaired = make_valid(mp)
        polygons.clear()
        _collect(repaired)
        mp = MultiPolygon(polygons)
    return mp


def heal_slivers(
    candidates: Sequence[SharedBoundaryCandidateInput],
    *,
    area_tolerance_sqm: float = 1e-4,
) -> SliverUnionResult:
    """Deterministically assign and union metric slivers into their longest-shared-boundary neighbors.

    Args:
        candidates: Candidate parcel inputs (both slivers and non-sliver neighbors) in projected metric CRS.
        area_tolerance_sqm: Maximum allowed discrepancy between `neighbor_before + sliver_area` and `neighbor_after`.

    Returns:
        `SliverUnionResult` containing ordered assignments, unassigned sliver IDs, updated neighbor `MultiPolygon`
        geometries, and recalculated `NeighborMetrics`.

    Raises:
        ValueError: If `area_tolerance_sqm` is negative or non-finite, or if a union violates area conservation.
    """
    if not math.isfinite(area_tolerance_sqm) or area_tolerance_sqm < 0.0:
        raise ValueError("area_tolerance_sqm must be >= 0 and finite")

    sorted_candidates = sorted(candidates, key=lambda c: c.parcel_id)
    current_geoms: dict[uuid.UUID, BaseGeometry] = {
        c.parcel_id: _coerce_geometry(c.geometry) for c in sorted_candidates
    }
    meta_by_id: dict[uuid.UUID, SharedBoundaryCandidateInput] = {
        c.parcel_id: c for c in sorted_candidates
    }

    sliver_inputs = [c for c in sorted_candidates if c.is_sliver]
    if not sliver_inputs:
        return SliverUnionResult(
            assignments=(),
            unassigned_sliver_ids=(),
            updated_geometries={},
            neighbor_metrics={},
        )

    assignments: list[SliverAssignment] = []
    unassigned_sliver_ids: list[uuid.UUID] = []
    updated_geometries: dict[uuid.UUID, MultiPolygon] = {}

    for sliver in sliver_inputs:
        if sliver.dispute_locked:
            unassigned_sliver_ids.append(sliver.parcel_id)
            continue

        sliver_geom = current_geoms[sliver.parcel_id]
        live_candidates = [
            SharedBoundaryCandidateInput(
                parcel_id=pid,
                geometry=current_geoms[pid],
                is_sliver=meta_by_id[pid].is_sliver,
                dispute_locked=meta_by_id[pid].dispute_locked,
            )
            for pid in sorted(current_geoms)
        ]

        match = identify_longest_shared_boundary_neighbor(
            sliver.parcel_id,
            sliver_geom,
            live_candidates,
            exclude_slivers=True,
            exclude_dispute_locked=True,
        )
        if match is None:
            unassigned_sliver_ids.append(sliver.parcel_id)
            continue

        neighbor_id = match.neighbor_parcel_id
        neighbor_geom_before = current_geoms[neighbor_id]
        neighbor_area_before = float(neighbor_geom_before.area)
        sliver_area = float(sliver_geom.area)

        merged_mp = _to_multipolygon(unary_union([neighbor_geom_before, sliver_geom]))
        neighbor_area_after = float(merged_mp.area)

        expected_area = neighbor_area_before + sliver_area
        if abs(expected_area - neighbor_area_after) > area_tolerance_sqm:
            raise ValueError(
                f"Area conservation violated merging sliver {sliver.parcel_id} into {neighbor_id}: "
                f"expected {expected_area:.6f} m², got {neighbor_area_after:.6f} m²"
            )

        current_geoms[neighbor_id] = merged_mp
        updated_geometries[neighbor_id] = merged_mp
        assignments.append(
            SliverAssignment(
                sliver_parcel_id=sliver.parcel_id,
                neighbor_parcel_id=neighbor_id,
                shared_length_m=float(match.shared_length_m),
                sliver_area_sqm=sliver_area,
                neighbor_area_before_sqm=neighbor_area_before,
                neighbor_area_after_sqm=neighbor_area_after,
            )
        )

    neighbor_metrics: dict[uuid.UUID, NeighborMetrics] = {}
    for neighbor_id, merged_mp in updated_geometries.items():
        compactness = evaluate_geometry_compactness(merged_mp)
        neighbor_metrics[neighbor_id] = NeighborMetrics(
            parcel_id=neighbor_id,
            area_sqm=float(compactness.area_sqm),
            perimeter_m=float(compactness.perimeter_m),
            polsby_popper=float(compactness.polsby_popper),
        )

    return SliverUnionResult(
        assignments=tuple(assignments),
        unassigned_sliver_ids=tuple(unassigned_sliver_ids),
        updated_geometries=updated_geometries,
        neighbor_metrics=neighbor_metrics,
    )


def heal_layer_slivers(
    parcels: Sequence[HarmonizedParcel],
    *,
    area_tolerance_sqm: float = 1e-4,
) -> SliverUnionResult:
    """Apply deterministic sliver healing in-place on `HarmonizedParcel` ORM records.

    - Receiving neighbor parcels receive the merged `MultiPolygon` in both `geometry_projected` (`EPSG:32643`) and
      `geometry_wgs84` (`EPSG:4326`), have their metrics recalculated, and are marked `AUTO_RESOLVED`.
    - Merged sliver records preserve their original geometries for auditability, record `merged_into_parcel_id` in
      `attributes`, and are marked `AUTO_RESOLVED`.
    - Unassigned slivers and dispute-locked parcels remain untouched.
    """
    parcel_by_id: dict[uuid.UUID, HarmonizedParcel] = {p.id: p for p in parcels}
    candidates = [
        SharedBoundaryCandidateInput(
            parcel_id=p.id,
            geometry=to_shape(p.geometry_projected),
            is_sliver=bool(p.is_sliver),
            dispute_locked=bool(p.dispute_locked),
        )
        for p in parcels
    ]

    result = heal_slivers(candidates, area_tolerance_sqm=area_tolerance_sqm)

    for neighbor_id, merged_mp in result.updated_geometries.items():
        neighbor = parcel_by_id[neighbor_id]
        neighbor.geometry_projected = from_shape(merged_mp, srid=PROJECTED_SRID)
        neighbor.geometry_wgs84 = from_shape(project_metric_geometry_to_wgs84(merged_mp), srid=4326)
        neighbor.recalculate_metrics(merged_mp)
        neighbor.mark_auto_resolved()

    for assignment in result.assignments:
        sliver_parcel = parcel_by_id[assignment.sliver_parcel_id]
        attrs = dict(sliver_parcel.attributes or {})
        attrs["merged_into_parcel_id"] = str(assignment.neighbor_parcel_id)
        sliver_parcel.attributes = attrs
        sliver_parcel.mark_auto_resolved()

    return result


def sliver_union_geometry_expr(neighbor_geom: Any, sliver_geom: Any) -> Any:
    """PostGIS expression unioning a sliver into a neighbor and normalizing to a projected MultiPolygon.

    Compiles to ``ST_Multi(ST_CollectionExtract(ST_Union(neighbor_geom, sliver_geom), 3))``.
    """
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_Union(neighbor_geom, sliver_geom),
            literal_column(str(_POLYGON_DIMENSION)),
        ),
        type_=Geometry(geometry_type="MULTIPOLYGON", srid=PROJECTED_SRID),
    )
