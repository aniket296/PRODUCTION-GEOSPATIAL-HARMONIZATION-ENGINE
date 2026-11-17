"""Full-stack E2E integration test for the cadastral harmonization & release bundle pipeline (Step 147/150)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from starlette.testclient import TestClient

from backend.api.app import app
from backend.db.session import get_async_session
from backend.export.bundle import build_harmonization_release_bundle
from backend.models.layer import LayerStatus, RawCadastralLayer
from backend.models.parcel import HarmonizedParcel
from backend.topology.recorder import TopologyConflictRecorder, project_metric_geometry_to_wgs84

DX, DY = 500_000.0, 3_000_000.0


def _rect(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return Polygon(
        [(x0 + DX, y0 + DY), (x1 + DX, y0 + DY), (x1 + DX, y1 + DY), (x0 + DX, y1 + DY), (x0 + DX, y0 + DY)]
    )


def _make_layer(layer_id: uuid.UUID) -> RawCadastralLayer:
    layer = RawCadastralLayer(
        name="Ward 14 E2E Master",
        filename="ward14_e2e.geojson",
        file_format="GEOJSON",
        storage_path="/tmp/ward14_e2e.geojson",
        source_crs="EPSG:32643",
        target_crs="EPSG:4326",
        status=LayerStatus.COMPLETED.value,
    )
    layer.id = layer_id
    return layer


def _make_parcel(
    layer_id: uuid.UUID,
    parcel_id: uuid.UUID,
    ident: str,
    poly: Polygon,
    *,
    sliver: bool = False,
) -> HarmonizedParcel:
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=parcel_id,
        layer_id=layer_id,
        parcel_identifier=ident,
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        polsby_popper_compactness=HarmonizedParcel.calculate_polsby_popper(float(poly.area), float(poly.length)),
        is_sliver=sliver,
    )


class TestFullStackHarmonizationPipelineE2E:
    """End-to-end journey: topology detection -> auto-resolve -> multi-format release bundle."""

    def test_e2e_topology_healing_and_release_bundle_zip_export(self) -> None:
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)

        id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
        id_c = uuid.UUID("00000000-0000-0000-0000-000000000003")
        id_d = uuid.UUID("00000000-0000-0000-0000-000000000004")
        id_s = uuid.UUID("00000000-0000-0000-0000-000000000099")

        p_a = _make_parcel(layer_id, id_a, "A", _rect(0.0, 0.0, 100.0, 100.0))
        p_b = _make_parcel(layer_id, id_b, "B", _rect(99.0, 0.0, 199.0, 100.0))
        p_s = _make_parcel(layer_id, id_s, "S", _rect(-0.1, 0.0, 0.0, 40.0), sliver=True)
        p_c = _make_parcel(layer_id, id_c, "C", _rect(400.0, 400.0, 500.0, 500.0))
        p_d = _make_parcel(layer_id, id_d, "D", _rect(490.0, 400.0, 590.0, 500.0))
        parcels = [p_a, p_b, p_c, p_d, p_s]

        recorder = TopologyConflictRecorder()
        errors = recorder.build_layer_errors(layer_id, parcels)
        for err in errors:
            if err.id is None:
                err.id = uuid.uuid4()
            if err.created_at is None:
                err.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # 1. Execute POST /auto-resolve
        layer_res = MagicMock()
        layer_res.scalar_one_or_none.return_value = layer
        parcel_res = MagicMock()
        parcel_res.scalars.return_value.all.return_value = parcels
        err_res = MagicMock()
        err_res.scalars.return_value.all.return_value = errors

        session = AsyncMock()
        session.execute.side_effect = [
            layer_res,
            parcel_res,
            err_res,
            layer_res,
            parcel_res,
            err_res,
        ]

        async def _override_session():
            yield session

        app.dependency_overrides[get_async_session] = _override_session
        try:
            with TestClient(app) as client:
                resolve_resp = client.post(
                    f"/api/v1/layers/{layer_id}/auto-resolve",
                    json={"resolved_by": "e2e-suite", "strict_verification": True},
                )
                assert resolve_resp.status_code == 200
                resolve_body = resolve_resp.json()
                assert resolve_body["status"] == "COMPLETED_WITH_DISPUTES"
                assert resolve_body["slivers_merged_count"] == 1
                assert resolve_body["micro_overlaps_resolved_count"] == 1

                # 2. Download multi-format release bundle ZIP via GET /{layer_id}/export/bundle
                bundle_resp = client.get(f"/api/v1/layers/{layer_id}/export/bundle?srid=32643")
                assert bundle_resp.status_code == 200
                assert bundle_resp.headers["content-type"].startswith("application/zip")
        finally:
            app.dependency_overrides.clear()

        with zipfile.ZipFile(io.BytesIO(bundle_resp.content), "r") as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert any(n.endswith(".geojson") for n in names)
            assert any(n.endswith(".gpkg") for n in names)
            assert any(n.endswith(".fgb") for n in names)

            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert manifest["layer"]["layer_id"] == str(layer_id)
            assert manifest["parcels"]["active_parcels"] == 4
            assert manifest["parcels"]["merged_slivers"] == 1
            assert "geojson" in manifest["artifacts"]
            assert "gpkg" in manifest["artifacts"]
            assert "fgb" in manifest["artifacts"]

        # 3. Verify deterministic in-memory bundle helper directly
        b1 = build_harmonization_release_bundle(
            layer=layer,
            parcels=parcels,
            topology_errors=errors,
            srid=32643,
        )
        b2 = build_harmonization_release_bundle(
            layer=layer,
            parcels=list(reversed(parcels)),
            topology_errors=list(reversed(errors)),
            srid=32643,
        )
        assert b1 == b2
