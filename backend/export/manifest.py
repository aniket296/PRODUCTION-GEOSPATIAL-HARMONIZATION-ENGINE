"""Deterministic harmonization audit certificate & lineage manifest generator."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from backend.models.layer import RawCadastralLayer
from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologicalError

AUDIT_MANIFEST_SCHEMA_VERSION: str = "1.0.0"


def _is_merged_sliver(parcel: HarmonizedParcel) -> bool:
    attrs = getattr(parcel, "attributes", None) or {}
    return bool(attrs.get("merged_into_parcel_id"))


def build_harmonization_audit_manifest(
    *,
    layer: RawCadastralLayer,
    parcels: Sequence[HarmonizedParcel],
    topology_errors: Sequence[TopologicalError],
    artifact_checksums: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic harmonization audit certificate dictionary."""
    sorted_parcels = sorted(parcels, key=lambda p: str(p.id))
    sorted_errors = sorted(topology_errors, key=lambda e: str(e.id))

    active_parcels = [p for p in sorted_parcels if not _is_merged_sliver(p)]
    merged_slivers = len(sorted_parcels) - len(active_parcels)
    disputed_parcels = sum(
        1 for p in active_parcels if bool(getattr(p, "dispute_locked", False)) or bool(getattr(p, "is_disputed", False))
    )
    total_active_area_sqm = round(
        sum(float(getattr(p, "metric_area_sqm", 0.0) or 0.0) for p in active_parcels),
        4,
    )

    type_counts = Counter(str(getattr(e, "error_type", "UNKNOWN")) for e in sorted_errors)
    severity_counts = Counter(str(getattr(e, "severity", "UNKNOWN")) for e in sorted_errors)
    resolved_errors = sum(1 for e in sorted_errors if bool(getattr(e, "is_resolved", False)))
    unresolved_errors = len(sorted_errors) - resolved_errors

    artifacts: dict[str, dict[str, str]] = {}
    if artifact_checksums:
        for fmt in sorted(artifact_checksums):
            artifacts[fmt] = {"sha256": str(artifact_checksums[fmt])}

    core_payload: dict[str, Any] = {
        "schema_version": AUDIT_MANIFEST_SCHEMA_VERSION,
        "layer": {
            "layer_id": str(layer.id),
            "name": layer.name,
            "filename": layer.filename,
            "file_format": layer.file_format,
            "source_crs": layer.source_crs,
            "target_crs": layer.target_crs,
            "status": str(layer.status),
        },
        "parcels": {
            "total_parcels": len(sorted_parcels),
            "active_parcels": len(active_parcels),
            "merged_slivers": merged_slivers,
            "disputed_parcels": disputed_parcels,
            "total_active_area_sqm": total_active_area_sqm,
        },
        "topology": {
            "total_errors": len(sorted_errors),
            "resolved_errors": resolved_errors,
            "unresolved_errors": unresolved_errors,
            "by_type": dict(sorted(type_counts.items())),
            "by_severity": dict(sorted(severity_counts.items())),
        },
        "artifacts": artifacts,
    }

    canonical_bytes = json.dumps(core_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

    return {
        **core_payload,
        "manifest_sha256": manifest_sha256,
    }


def export_audit_manifest_bytes(
    *,
    layer: RawCadastralLayer,
    parcels: Sequence[HarmonizedParcel],
    topology_errors: Sequence[TopologicalError],
    artifact_checksums: Mapping[str, str] | None = None,
) -> bytes:
    """Serialize harmonization audit manifest as deterministic UTF-8 JSON bytes."""
    manifest = build_harmonization_audit_manifest(
        layer=layer,
        parcels=parcels,
        topology_errors=topology_errors,
        artifact_checksums=artifact_checksums,
    )
    return json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
