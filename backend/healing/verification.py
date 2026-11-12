"""Post-resolution verification and audit timestamping for deterministic cadastral healing (Step 103/150).

Directive Sections 20-25:
- Re-verifies healed active parcels in projected metric coordinates (`EPSG:32643`) to ensure:
  1. Every parcel geometry is valid, non-empty, and has positive metric area.
  2. Zero residual `AUTO_RESOLVABLE` overlaps, slivers, boundary gaps, duplicate vertices, or dangling nodes remain
     among active parcels (excluding merged slivers whose original geometry is retained solely for auditability).
  3. All `CRITICAL_DISPUTE` errors remain unresolved (`is_resolved=False`, `resolved_at=None`).
- Stamps every resolved `AUTO_RESOLVABLE` `TopologicalError` via `TopologicalError.resolve(...)` with deterministic
  `resolution_method`, `resolved_by`, `resolution_details` (containing ISO-8601 `verified_at` and post-healing metrics),
  and monotonic timezone-aware UTC audit timestamps (`resolved_at >= created_at`, `updated_at >= resolved_at`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from geoalchemy2.shape import to_shape

from backend.healing.orchestrator import HealingPipelineResult
from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.topology.recorder import TopologyConflictRecorder

DEFAULT_RESOLVED_BY: str = "auto-healing-engine"

_RESOLUTION_METHOD_BY_ERROR_TYPE: dict[str, str] = {
    TopologyErrorType.SLIVER.value: "SLIVER_UNION",
    TopologyErrorType.OVERLAP.value: "MICRO_OVERLAP_CENTERLINE_SPLIT",
    TopologyErrorType.GAP.value: "BOUNDARY_SNAP",
    TopologyErrorType.DANGLING_NODE.value: "BOUNDARY_SNAP",
    TopologyErrorType.DUPLICATE_VERTEX.value: "GRID_SNAP",
}


class PostResolutionVerificationError(ValueError):
    """Raised when strict post-resolution verification detects invalid geometry or residual auto-resolvable errors."""


@dataclass(frozen=True, slots=True)
class PostResolutionVerificationResult:
    """Audit summary returned after verifying post-healing cadastral topology."""

    verified_at: datetime
    resolved_error_ids: tuple[uuid.UUID, ...]
    remaining_dispute_error_ids: tuple[uuid.UUID, ...]
    residual_auto_resolvable_count: int
    residual_critical_dispute_count: int
    valid_parcel_count: int
    invalid_parcel_ids: tuple[uuid.UUID, ...]
    passed: bool


def _is_merged_sliver(
    parcel: HarmonizedParcel,
    merged_sliver_ids: set[uuid.UUID],
) -> bool:
    if parcel.id in merged_sliver_ids:
        return True
    attrs = parcel.attributes or {}
    return bool(attrs.get("merged_into_parcel_id"))


def _error_signature(err: TopologicalError) -> tuple[str, tuple[uuid.UUID | None, uuid.UUID | None]]:
    p1 = err.primary_parcel_id
    p2 = err.secondary_parcel_id
    if p1 is not None and p2 is not None and p1 > p2:
        pair = (p2, p1)
    else:
        pair = (p1, p2)
    return (err.error_type, pair)


def _method_for_error(
    err: TopologicalError,
    pipeline_result: HealingPipelineResult | None,
) -> str:
    if pipeline_result is not None and err.error_type == TopologyErrorType.SLIVER.value:
        return "SLIVER_UNION"
    if pipeline_result is not None and err.error_type == TopologyErrorType.OVERLAP.value:
        return "MICRO_OVERLAP_CENTERLINE_SPLIT"
    return _RESOLUTION_METHOD_BY_ERROR_TYPE.get(err.error_type, "AUTO_HEALING_PIPELINE")


def _details_for_error(
    err: TopologicalError,
    verified_at: datetime,
    pipeline_result: HealingPipelineResult | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "verified_at": verified_at.isoformat(),
        "error_type": err.error_type,
        "affected_area_sqm": float(err.affected_area_sqm or 0.0),
    }
    if pipeline_result is not None:
        if err.error_type == TopologyErrorType.SLIVER.value:
            for assignment in pipeline_result.sliver_result.assignments:
                if assignment.sliver_parcel_id == err.primary_parcel_id:
                    details["merged_into_parcel_id"] = str(assignment.neighbor_parcel_id)
                    details["shared_length_m"] = assignment.shared_length_m
                    details["neighbor_area_after_sqm"] = assignment.neighbor_area_after_sqm
                    break
        elif err.error_type == TopologyErrorType.OVERLAP.value:
            sig = _error_signature(err)[1]
            for split in pipeline_result.micro_overlap_result.resolved_pairs:
                if (split.parcel_a_id, split.parcel_b_id) == sig:
                    details["intersection_area_sqm"] = split.intersection_area_sqm
                    details["area_a_after_sqm"] = split.area_a_after_sqm
                    details["area_b_after_sqm"] = split.area_b_after_sqm
                    break
    return details


def verify_and_audit_healing(
    parcels: Sequence[HarmonizedParcel],
    topology_errors: Sequence[TopologicalError],
    *,
    pipeline_result: HealingPipelineResult | None = None,
    recorder: TopologyConflictRecorder | None = None,
    resolved_by: str = DEFAULT_RESOLVED_BY,
    strict: bool = True,
) -> PostResolutionVerificationResult:
    """Verify healed parcel topology and stamp resolved `TopologicalError` audit records.

    When `strict=True`, any invalid geometry or residual `AUTO_RESOLVABLE` defect raises
    `PostResolutionVerificationError` BEFORE mutating any audit record.
    """
    verified_at = datetime.now(timezone.utc)
    invalid_ids: list[uuid.UUID] = []
    valid_count = 0

    for parcel in sorted(parcels, key=lambda p: p.id):
        geom = to_shape(parcel.geometry_projected)
        if geom.is_empty or not geom.is_valid or geom.area <= 0.0:
            invalid_ids.append(parcel.id)
        else:
            valid_count += 1

    merged_sliver_ids: set[uuid.UUID] = set()
    if pipeline_result is not None:
        merged_sliver_ids = {a.sliver_parcel_id for a in pipeline_result.sliver_result.assignments}

    active_parcels = [p for p in parcels if not _is_merged_sliver(p, merged_sliver_ids)]

    layer_id = (
        active_parcels[0].layer_id
        if active_parcels
        else (parcels[0].layer_id if parcels else uuid.UUID(int=0))
    )
    active_recorder = recorder or TopologyConflictRecorder()
    residual_errors = active_recorder.build_layer_errors(layer_id, active_parcels)

    residual_auto = [e for e in residual_errors if e.severity == TopologySeverity.AUTO_RESOLVABLE.value]
    residual_critical = [e for e in residual_errors if e.severity == TopologySeverity.CRITICAL_DISPUTE.value]
    residual_sigs = {_error_signature(e) for e in residual_errors}

    passed = (not invalid_ids) and (len(residual_auto) == 0)

    if strict and not passed:
        raise PostResolutionVerificationError(
            f"Post-resolution verification failed: {len(residual_auto)} residual AUTO_RESOLVABLE error(s), "
            f"{len(invalid_ids)} invalid parcel(s)"
        )

    resolved_ids: list[uuid.UUID] = []
    dispute_ids: list[uuid.UUID] = []

    for err in sorted(topology_errors, key=lambda e: e.id or uuid.UUID(int=0)):
        if err.severity == TopologySeverity.CRITICAL_DISPUTE.value:
            if err.id is not None:
                dispute_ids.append(err.id)
            continue

        if err.severity != TopologySeverity.AUTO_RESOLVABLE.value:
            continue

        if _error_signature(err) in residual_sigs:
            continue

        if not err.is_resolved:
            method = _method_for_error(err, pipeline_result)
            details = _details_for_error(err, verified_at, pipeline_result)
            err.resolve(method=method, resolved_by=resolved_by, details=details)
            stamp = datetime.now(timezone.utc)
            if err.created_at is not None and stamp < err.created_at:
                stamp = err.created_at
            err.resolved_at = stamp
            err.updated_at = stamp

        if err.id is not None:
            resolved_ids.append(err.id)

    return PostResolutionVerificationResult(
        verified_at=verified_at,
        resolved_error_ids=tuple(resolved_ids),
        remaining_dispute_error_ids=tuple(dispute_ids),
        residual_auto_resolvable_count=len(residual_auto),
        residual_critical_dispute_count=len(residual_critical),
        valid_parcel_count=valid_count,
        invalid_parcel_ids=tuple(invalid_ids),
        passed=passed,
    )


async def verify_layer_post_resolution(
    session: Any,
    parcels: Sequence[HarmonizedParcel],
    topology_errors: Sequence[TopologicalError],
    *,
    pipeline_result: HealingPipelineResult | None = None,
    recorder: TopologyConflictRecorder | None = None,
    resolved_by: str = DEFAULT_RESOLVED_BY,
    strict: bool = True,
) -> PostResolutionVerificationResult:
    """Verify healed parcels, update `TopologicalError` audit records, and flush `session`."""
    result = verify_and_audit_healing(
        parcels,
        topology_errors,
        pipeline_result=pipeline_result,
        recorder=recorder,
        resolved_by=resolved_by,
        strict=strict,
    )
    await session.flush()
    return result
