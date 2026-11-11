"""Critical-dispute detection, locking, and locked-geometry preservation guard (Step 95/150).

Directive Sections 15, 20-23, 25:
- A pairwise overlap whose independent overlap percentage exceeds `max_overlap_percent_threshold` (default 2.0%) on
  EITHER parcel is a `CRITICAL_DISPUTE`. Both parcels are dispute-locked via `HarmonizedParcel.lock_dispute`, with an
  auditable `dispute_reason` and the sorted list of conflicting parcel IDs in `attributes["disputed_with_parcel_ids"]`.
- Locking never modifies geometry. All overlap metrics are computed on projected metric geometries (EPSG:32643).
- Locked parcels are excluded from automated healing (`filter_healable_parcels`), and a snapshot/assert guard detects
  any post-healing modification of a locked parcel's projected or WGS 84 geometry.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from geoalchemy2.shape import to_shape
from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry

from backend.core.config import get_settings
from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologyErrorType, TopologySeverity
from backend.topology.intersection import extract_polygonal_intersection
from backend.topology.overlap_percentage import evaluate_pair_overlap_metrics
from backend.topology.severity import classify_topology_severity


class DisputedGeometryModifiedError(ValueError):
    """Raised when the geometry of a dispute-locked parcel was modified or removed."""


@dataclass(frozen=True, slots=True)
class DisputeCandidateInput:
    """A parcel evaluated for critical-dispute detection (projected metric geometry)."""

    parcel_id: uuid.UUID
    geometry: BaseGeometry | str
    dispute_locked: bool = False


@dataclass(frozen=True, slots=True)
class CriticalDisputePair:
    """A canonical (`parcel_a_id < parcel_b_id`) pair whose overlap exceeds the threshold on either parcel."""

    parcel_a_id: uuid.UUID
    parcel_b_id: uuid.UUID
    intersection_area_sqm: float
    overlap_percentage_a: float
    overlap_percentage_b: float


@dataclass(frozen=True, slots=True)
class DisputeProtectionResult:
    """Outcome of critical-dispute detection.

    Attributes:
        critical_pairs: Critical pairs in ascending `(parcel_a_id, parcel_b_id)` order.
        locked_parcel_ids: All parcels that are dispute-locked after evaluation (pre-locked + newly locked), sorted.
        newly_locked_parcel_ids: Parcels locked by this evaluation that were not locked before, sorted.
    """

    critical_pairs: tuple[CriticalDisputePair, ...]
    locked_parcel_ids: tuple[uuid.UUID, ...]
    newly_locked_parcel_ids: tuple[uuid.UUID, ...]


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _resolve_threshold(threshold_percent: float | None) -> float:
    threshold = (
        get_settings().max_overlap_percent_threshold if threshold_percent is None else float(threshold_percent)
    )
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 100.0:
        raise ValueError("threshold_percent must be within [0, 100] and finite")
    return threshold


def detect_critical_disputes(
    candidates: Sequence[DisputeCandidateInput],
    *,
    threshold_percent: float | None = None,
) -> DisputeProtectionResult:
    """Detect all `CRITICAL_DISPUTE` overlap pairs deterministically (independent of input order).

    Raises:
        ValueError: If `threshold_percent` is outside `[0, 100]` or non-finite.
    """
    threshold = _resolve_threshold(threshold_percent)

    ordered = sorted(candidates, key=lambda c: c.parcel_id)
    geoms = [_coerce_geometry(c.geometry) for c in ordered]
    pre_locked = {c.parcel_id for c in ordered if c.dispute_locked}

    pairs: list[CriticalDisputePair] = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            geom_a, geom_b = geoms[i], geoms[j]
            if not geom_a.intersects(geom_b):
                continue
            if extract_polygonal_intersection(geom_a, geom_b).area <= 0.0:
                continue
            id_a, id_b = ordered[i].parcel_id, ordered[j].parcel_id
            metrics = evaluate_pair_overlap_metrics(geom_a, geom_b, parcel_a_id=id_a, parcel_b_id=id_b)
            severity = classify_topology_severity(
                TopologyErrorType.OVERLAP,
                metrics.overlap_percentage_primary,
                metrics.overlap_percentage_secondary,
                threshold_percent=threshold,
            )
            if severity is TopologySeverity.CRITICAL_DISPUTE:
                pairs.append(
                    CriticalDisputePair(
                        parcel_a_id=id_a,
                        parcel_b_id=id_b,
                        intersection_area_sqm=float(metrics.intersection_area_sqm),
                        overlap_percentage_a=float(metrics.overlap_percentage_primary),
                        overlap_percentage_b=float(metrics.overlap_percentage_secondary),
                    )
                )

    disputed = {p.parcel_a_id for p in pairs} | {p.parcel_b_id for p in pairs}
    return DisputeProtectionResult(
        critical_pairs=tuple(pairs),
        locked_parcel_ids=tuple(sorted(pre_locked | disputed)),
        newly_locked_parcel_ids=tuple(sorted(disputed - pre_locked)),
    )


def apply_dispute_locks(
    parcels: Sequence[HarmonizedParcel],
    *,
    threshold_percent: float | None = None,
) -> DisputeProtectionResult:
    """Detect critical disputes among ORM parcels and lock every involved parcel without touching geometry."""
    parcel_by_id = {p.id: p for p in parcels}
    candidates = [
        DisputeCandidateInput(
            parcel_id=p.id,
            geometry=to_shape(p.geometry_projected),
            dispute_locked=bool(p.dispute_locked),
        )
        for p in parcels
    ]
    threshold = _resolve_threshold(threshold_percent)
    result = detect_critical_disputes(candidates, threshold_percent=threshold)

    # parcel_id -> list of (other_id, pct_self, pct_other), built in canonical pair order.
    conflicts: dict[uuid.UUID, list[tuple[uuid.UUID, float, float]]] = {}
    for pair in result.critical_pairs:
        conflicts.setdefault(pair.parcel_a_id, []).append(
            (pair.parcel_b_id, pair.overlap_percentage_a, pair.overlap_percentage_b)
        )
        conflicts.setdefault(pair.parcel_b_id, []).append(
            (pair.parcel_a_id, pair.overlap_percentage_b, pair.overlap_percentage_a)
        )

    for parcel_id in sorted(conflicts):
        entries = sorted(conflicts[parcel_id], key=lambda e: e[0])
        other_id, pct_self, pct_other = entries[0]
        reason = (
            f"CRITICAL_DISPUTE overlap with parcel {other_id}: {pct_self:.4f}% of this parcel, "
            f"{pct_other:.4f}% of the other (threshold {threshold:.4f}%)"
        )
        parcel = parcel_by_id[parcel_id]
        parcel.lock_dispute(reason)

        attrs = dict(parcel.attributes or {})
        existing = set(attrs.get("disputed_with_parcel_ids") or [])
        attrs["disputed_with_parcel_ids"] = sorted(existing | {str(e[0]) for e in entries})
        parcel.attributes = attrs

    return result


def filter_healable_parcels(parcels: Sequence[HarmonizedParcel]) -> tuple[HarmonizedParcel, ...]:
    """Return parcels eligible for automated healing (not dispute-locked), preserving input order."""
    return tuple(p for p in parcels if not p.dispute_locked)


def _wkb_pair(parcel: HarmonizedParcel) -> tuple[bytes, bytes]:
    return bytes(parcel.geometry_projected.data), bytes(parcel.geometry_wgs84.data)


def snapshot_locked_geometries(parcels: Sequence[HarmonizedParcel]) -> dict[uuid.UUID, tuple[bytes, bytes]]:
    """Capture `(projected WKB, WGS 84 WKB)` for every dispute-locked parcel."""
    return {p.id: _wkb_pair(p) for p in parcels if p.dispute_locked}


def assert_locked_geometries_preserved(
    parcels: Sequence[HarmonizedParcel],
    snapshot: Mapping[uuid.UUID, tuple[bytes, bytes]],
) -> None:
    """Verify every snapshotted locked parcel is still present with byte-identical geometries.

    Raises:
        DisputedGeometryModifiedError: Listing every locked parcel that is missing or whose geometry changed.
    """
    current = {p.id: p for p in parcels}
    violations: list[str] = []
    for parcel_id in sorted(snapshot):
        parcel = current.get(parcel_id)
        if parcel is None:
            violations.append(f"{parcel_id} (missing)")
            continue
        proj_before, wgs_before = snapshot[parcel_id]
        proj_now, wgs_now = _wkb_pair(parcel)
        changed = []
        if proj_now != proj_before:
            changed.append("geometry_projected")
        if wgs_now != wgs_before:
            changed.append("geometry_wgs84")
        if changed:
            violations.append(f"{parcel_id} ({', '.join(changed)} modified)")

    if violations:
        raise DisputedGeometryModifiedError(
            "Dispute-locked parcel geometry must never be modified: " + "; ".join(violations)
        )
