"""Atomic healing pipeline orchestrator with full rollback on invalid geometry (Step 101/150).

Directive Sections 20-25. Stages run in a fixed, deterministic order on projected metric geometries (EPSG:32643):

1. Critical-dispute detection and locking (`apply_dispute_locks`)
2. Sliver assignment and union (`heal_layer_slivers`)
3. Micro-overlap centerline splitting (`heal_layer_micro_overlaps`); merged sliver records are excluded because their
   original geometry is preserved only for auditability
4. Metric boundary snapping (`snap_layer_boundaries`)
5. 1 mm grid normalization (`normalize_layer_grid`)

The whole run is atomic. Any stage failure, any parcel rejected by snapping or grid normalization, any modified
dispute-locked geometry, or any invalid final geometry restores every parcel to its exact pre-run state and raises
`HealingTransactionRollbackError`. `execute_healing_transaction` additionally rolls back the database session.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from geoalchemy2.shape import to_shape

from backend.healing.boundary_snap import BoundarySnapBatchResult, snap_layer_boundaries
from backend.healing.dispute_protection import (
    DisputedGeometryModifiedError,
    DisputeProtectionResult,
    apply_dispute_locks,
    assert_locked_geometries_preserved,
    snapshot_locked_geometries,
)
from backend.healing.grid_snap import GridSnapBatchResult, normalize_layer_grid
from backend.healing.micro_overlap import MicroOverlapBatchResult, heal_layer_micro_overlaps
from backend.healing.sliver_union import SliverUnionResult, heal_layer_slivers
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus


class HealingTransactionRollbackError(ValueError):
    """Raised after a healing run failed and every parcel was restored to its pre-run state."""


@dataclass(frozen=True, slots=True)
class ParcelStateSnapshot:
    """Complete mutable healing-relevant state of a parcel."""

    geometry_projected: Any
    geometry_wgs84: Any
    metric_area_sqm: float | None
    metric_perimeter_m: float | None
    polsby_popper_compactness: float | None
    is_sliver: bool
    is_disputed: bool
    dispute_locked: bool
    resolution_status: str
    attributes: dict[str, Any] | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class HealingPipelineResult:
    """Outcome of a successful healing run."""

    dispute_result: DisputeProtectionResult
    sliver_result: SliverUnionResult
    micro_overlap_result: MicroOverlapBatchResult
    snap_result: BoundarySnapBatchResult
    grid_result: GridSnapBatchResult
    auto_resolved_parcel_ids: tuple[uuid.UUID, ...]
    disputed_parcel_ids: tuple[uuid.UUID, ...]


def capture_parcel_snapshots(parcels: Sequence[HarmonizedParcel]) -> dict[uuid.UUID, ParcelStateSnapshot]:
    """Capture the exact healing-relevant state of every parcel."""
    return {
        p.id: ParcelStateSnapshot(
            geometry_projected=p.geometry_projected,
            geometry_wgs84=p.geometry_wgs84,
            metric_area_sqm=p.metric_area_sqm,
            metric_perimeter_m=p.metric_perimeter_m,
            polsby_popper_compactness=p.polsby_popper_compactness,
            is_sliver=p.is_sliver,
            is_disputed=p.is_disputed,
            dispute_locked=p.dispute_locked,
            resolution_status=p.resolution_status,
            attributes=copy.deepcopy(p.attributes),
            updated_at=p.updated_at,
        )
        for p in parcels
    }


def restore_parcel_snapshots(
    parcels: Sequence[HarmonizedParcel],
    snapshots: dict[uuid.UUID, ParcelStateSnapshot],
) -> None:
    """Restore every snapshotted parcel to its captured state."""
    for p in parcels:
        snap = snapshots.get(p.id)
        if snap is None:
            continue
        p.geometry_projected = snap.geometry_projected
        p.geometry_wgs84 = snap.geometry_wgs84
        p.metric_area_sqm = snap.metric_area_sqm
        p.metric_perimeter_m = snap.metric_perimeter_m
        p.polsby_popper_compactness = snap.polsby_popper_compactness
        p.is_sliver = snap.is_sliver
        p.is_disputed = snap.is_disputed
        p.dispute_locked = snap.dispute_locked
        p.resolution_status = snap.resolution_status
        p.attributes = copy.deepcopy(snap.attributes)
        p.updated_at = snap.updated_at


def _run_stages(parcels: Sequence[HarmonizedParcel]) -> HealingPipelineResult:
    status_before = {p.id: p.resolution_status for p in parcels}

    dispute_result = apply_dispute_locks(parcels)
    locked_snapshot = snapshot_locked_geometries(parcels)

    sliver_result = heal_layer_slivers(parcels)
    merged_sliver_ids = {a.sliver_parcel_id for a in sliver_result.assignments}
    active = [p for p in parcels if p.id not in merged_sliver_ids]

    micro_result = heal_layer_micro_overlaps(active)

    snap_result = snap_layer_boundaries(active)
    if snap_result.rejected_parcel_ids:
        ids = ", ".join(str(i) for i in snap_result.rejected_parcel_ids)
        raise HealingTransactionRollbackError(f"Boundary snapping produced invalid geometry for parcel(s): {ids}")

    grid_result = normalize_layer_grid(active)
    if grid_result.rejected_parcel_ids:
        ids = ", ".join(str(i) for i in grid_result.rejected_parcel_ids)
        raise HealingTransactionRollbackError(f"Grid normalization produced invalid geometry for parcel(s): {ids}")

    assert_locked_geometries_preserved(parcels, locked_snapshot)

    invalid = sorted(
        str(p.id)
        for p in parcels
        if (geom := to_shape(p.geometry_projected)).is_empty or not geom.is_valid
    )
    if invalid:
        raise HealingTransactionRollbackError(f"Invalid final geometry for parcel(s): {', '.join(invalid)}")

    auto_resolved = tuple(
        sorted(
            p.id
            for p in parcels
            if p.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
            and status_before[p.id] != ParcelResolutionStatus.AUTO_RESOLVED.value
        )
    )
    return HealingPipelineResult(
        dispute_result=dispute_result,
        sliver_result=sliver_result,
        micro_overlap_result=micro_result,
        snap_result=snap_result,
        grid_result=grid_result,
        auto_resolved_parcel_ids=auto_resolved,
        disputed_parcel_ids=dispute_result.locked_parcel_ids,
    )


def execute_layer_healing_pipeline(parcels: Sequence[HarmonizedParcel]) -> HealingPipelineResult:
    """Run the full deterministic healing pipeline atomically over `parcels`.

    Raises:
        HealingTransactionRollbackError: If any stage fails. All parcels are restored to their pre-run state.
    """
    snapshots = capture_parcel_snapshots(parcels)
    try:
        return _run_stages(parcels)
    except HealingTransactionRollbackError:
        restore_parcel_snapshots(parcels, snapshots)
        raise
    except (DisputedGeometryModifiedError, ValueError) as exc:
        restore_parcel_snapshots(parcels, snapshots)
        raise HealingTransactionRollbackError(f"Healing aborted and rolled back: {exc}") from exc


async def execute_healing_transaction(session: Any, parcels: Sequence[HarmonizedParcel]) -> HealingPipelineResult:
    """Run the healing pipeline and flush on success; roll back the session and parcels on failure.

    The caller owns the final commit.
    """
    try:
        result = execute_layer_healing_pipeline(parcels)
    except HealingTransactionRollbackError:
        await session.rollback()
        raise
    await session.flush()
    return result
