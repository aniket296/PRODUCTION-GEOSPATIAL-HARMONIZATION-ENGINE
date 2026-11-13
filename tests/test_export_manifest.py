"""Red-phase tests for harmonization audit certificate & lineage manifest (Step 117/150)."""

from __future__ import annotations

import json
import uuid

from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.export.manifest import (
    AUDIT_MANIFEST_SCHEMA_VERSION,
    build_harmonization_audit_manifest,
    export_audit_manifest_bytes,
)
from backend.models.layer import LayerStatus, RawCadastralLayer
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.models.topology import (
    ErrorSeverity,
    TopologyError,
    TopologyErrorType,
)
from backend.topology.recorder import project_metric_geometry_to_wgs84


def _make_layer(layer_id: uuid.UUID) -> RawCadastralLayer:
    layer = RawCadastralLayer(
        name="Sector 22 Master",
        filename="sector22.dxf",
        file_format="DXF",
        storage_path="/tmp/sector22.dxf",
        source_crs="EPSG:32643",
        target_crs="EPSG:4326",
        status=LayerStatus.COMPLETED.value,
    )
    layer.id = layer_id
    return layer


def _make_parcel(
    layer_id: uuid.UUID,
    ident: str,
    *,
    disputed: bool = False,
    merged: bool = False,
) -> HarmonizedParcel:
    poly = Polygon([(500000, 3000000), (500100, 3000000), (500100, 3000100), (500000, 3000100), (500000, 3000000)])
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=uuid.uuid4(),
        layer_id=layer_id,
        parcel_identifier=ident,
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        polsby_popper_compactness=HarmonizedParcel.calculate_polsby_popper(float(poly.area), float(poly.length)),
        is_sliver=merged,
        is_disputed=disputed,
        dispute_locked=disputed,
        resolution_status=ParcelResolutionStatus.AUTO_RESOLVED.value,
        attributes={"merged_into_parcel_id": "P-1"} if merged else {},
    )


def _make_error(
    layer_id: uuid.UUID,
    err_type: TopologyErrorType,
    severity: ErrorSeverity,
    resolved: bool,
) -> TopologyError:
    return TopologyError(
        id=uuid.uuid4(),
        layer_id=layer_id,
        error_type=err_type.value,
        severity=severity.value,
        is_resolved=resolved,
        error_area_sqm=1.25,
    )


class TestHarmonizationAuditManifest:
    """Tests for deterministic audit certificate and lineage manifest generation."""

    def test_builds_audit_manifest_with_parcel_and_topology_counts(self) -> None:
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)
        parcels = [
            _make_parcel(layer_id, "P-1", disputed=False, merged=False),
            _make_parcel(layer_id, "P-2", disputed=True, merged=False),
            _make_parcel(layer_id, "P-SLIVER", disputed=False, merged=True),
        ]
        errors = [
            _make_error(layer_id, TopologyErrorType.SLIVER, ErrorSeverity.LOW, resolved=True),
            _make_error(layer_id, TopologyErrorType.OVERLAP, ErrorSeverity.CRITICAL, resolved=False),
            _make_error(layer_id, TopologyErrorType.GAP, ErrorSeverity.MEDIUM, resolved=True),
        ]

        manifest = build_harmonization_audit_manifest(
            layer=layer,
            parcels=parcels,
            topology_errors=errors,
            artifact_checksums={"geojson": "a" * 64, "gpkg": "b" * 64},
        )

        assert manifest["schema_version"] == AUDIT_MANIFEST_SCHEMA_VERSION
        assert manifest["layer"]["layer_id"] == str(layer_id)
        assert manifest["layer"]["name"] == "Sector 22 Master"
        assert manifest["layer"]["source_crs"] == "EPSG:32643"
        assert manifest["layer"]["target_crs"] == "EPSG:4326"

        p_summary = manifest["parcels"]
        assert p_summary["total_parcels"] == 3
        assert p_summary["active_parcels"] == 2
        assert p_summary["merged_slivers"] == 1
        assert p_summary["disputed_parcels"] == 1
        assert p_summary["total_active_area_sqm"] == 20_000.0

        t_summary = manifest["topology"]
        assert t_summary["total_errors"] == 3
        assert t_summary["resolved_errors"] == 2
        assert t_summary["unresolved_errors"] == 1
        assert t_summary["by_type"]["SLIVER"] == 1
        assert t_summary["by_type"]["OVERLAP"] == 1
        assert t_summary["by_severity"]["CRITICAL"] == 1

        assert manifest["artifacts"]["geojson"]["sha256"] == "a" * 64
        assert manifest["artifacts"]["gpkg"]["sha256"] == "b" * 64
        assert len(manifest["manifest_sha256"]) == 64

    def test_manifest_bytes_are_deterministic_regardless_of_input_order(self) -> None:
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)
        p1 = _make_parcel(layer_id, "P-1")
        p2 = _make_parcel(layer_id, "P-2")
        e1 = _make_error(layer_id, TopologyErrorType.SLIVER, ErrorSeverity.LOW, resolved=True)
        e2 = _make_error(layer_id, TopologyErrorType.GAP, ErrorSeverity.MEDIUM, resolved=False)

        b1 = export_audit_manifest_bytes(layer=layer, parcels=[p1, p2], topology_errors=[e1, e2])
        b2 = export_audit_manifest_bytes(layer=layer, parcels=[p2, p1], topology_errors=[e2, e1])

        assert b1 == b2
        parsed = json.loads(b1.decode("utf-8"))
        assert len(parsed["manifest_sha256"]) == 64
