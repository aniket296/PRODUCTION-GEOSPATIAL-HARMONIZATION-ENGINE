"""Red-phase tests for harmonized export download and MinIO presign APIs (Step 115/150)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from starlette.testclient import TestClient

from backend.api.app import app
from backend.db.session import get_async_session
from backend.models.layer import LayerStatus, RawCadastralLayer
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.storage.minio_client import get_storage_client
from backend.topology.recorder import project_metric_geometry_to_wgs84


def _rect(x0: float, y0: float, x1: float, y1: float, dx: float, dy: float) -> Polygon:
    return Polygon(
        [(x0 + dx, y0 + dy), (x1 + dx, y0 + dy), (x1 + dx, y1 + dy), (x0 + dx, y1 + dy), (x0 + dx, y0 + dy)]
    )


def _make_layer(layer_id: uuid.UUID) -> RawCadastralLayer:
    layer = RawCadastralLayer(
        name="Ward 14 Harmonized",
        filename="ward14.geojson",
        file_format="GEOJSON",
        storage_path="/tmp/ward14.geojson",
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
    dx: float = 500_000.0,
    dy: float = 3_000_000.0,
) -> HarmonizedParcel:
    poly = _rect(0.0, 0.0, 100.0, 100.0, dx=dx, dy=dy)
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
        resolution_status=ParcelResolutionStatus.AUTO_RESOLVED.value,
        attributes={"ward": "14"},
    )


def _mock_session_with_parcels(
    layer: RawCadastralLayer | None,
    parcels: list[HarmonizedParcel],
) -> AsyncMock:
    session = AsyncMock()

    layer_res = MagicMock()
    layer_res.scalar_one_or_none.return_value = layer

    parcel_res = MagicMock()
    parcel_scalars = MagicMock()
    parcel_scalars.all.return_value = parcels
    parcel_res.scalars.return_value = parcel_scalars

    if layer is None:
        session.execute.return_value = layer_res
    else:
        session.execute.side_effect = [layer_res, parcel_res]
    return session


class TestLayerExportDownloadAndPresignAPI:
    """Tests for GET /{layer_id}/export and POST /{layer_id}/export/presign."""

    def test_direct_download_geojson_gpkg_and_fgb(self) -> None:
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)
        parcels = [_make_parcel(layer_id, uuid.uuid4(), "P-14-01")]

        try:
            # 1. GeoJSON
            session = _mock_session_with_parcels(layer, parcels)
            app.dependency_overrides[get_async_session] = lambda: session
            with TestClient(app) as client:
                resp = client.get(f"/api/v1/layers/{layer_id}/export?format=geojson")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("application/geo+json")
            assert ".geojson" in resp.headers["content-disposition"]
            payload = json.loads(resp.content.decode("utf-8"))
            assert payload["type"] == "FeatureCollection"
            assert len(payload["features"]) == 1
            assert payload["features"][0]["properties"]["parcel_identifier"] == "P-14-01"

            # 2. GeoPackage (EPSG:32643)
            session = _mock_session_with_parcels(layer, parcels)
            app.dependency_overrides[get_async_session] = lambda: session
            with TestClient(app) as client:
                resp = client.get(f"/api/v1/layers/{layer_id}/export?format=gpkg&srid=32643")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("application/geopackage+sqlite3")
            assert ".gpkg" in resp.headers["content-disposition"]
            assert resp.content.startswith(b"SQLite format 3\x00")

            # 3. FlatGeobuf
            session = _mock_session_with_parcels(layer, parcels)
            app.dependency_overrides[get_async_session] = lambda: session
            with TestClient(app) as client:
                resp = client.get(f"/api/v1/layers/{layer_id}/export?format=fgb")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("application/vnd.flatgeobuf")
            assert ".fgb" in resp.headers["content-disposition"]
            assert resp.content[:3] == b"fgb"
        finally:
            app.dependency_overrides.clear()

    def test_presign_stages_export_in_minio_and_returns_metadata(self) -> None:
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)
        parcels = [_make_parcel(layer_id, uuid.uuid4(), "P-14-02")]

        mock_storage = MagicMock()
        mock_storage.object_exists.return_value = False
        mock_storage.get_presigned_download_url.return_value = (
            f"https://minio.local/cadastral-harmonized/exports/{layer_id}/ward_14_harmonized_epsg32643_harmonized.gpkg?sig=1"
        )

        try:
            session = _mock_session_with_parcels(layer, parcels)
            app.dependency_overrides[get_async_session] = lambda: session
            app.dependency_overrides[get_storage_client] = lambda: mock_storage
            with TestClient(app) as client:
                resp = client.post(
                    f"/api/v1/layers/{layer_id}/export/presign",
                    json={
                        "format": "gpkg",
                        "srid": 32643,
                        "expires_seconds": 900,
                        "force_rebuild": False,
                    },
                )

            assert resp.status_code == 200
            body = resp.json()
            assert body["layer_id"] == str(layer_id)
            assert body["format"] == "gpkg"
            assert body["feature_count"] == 1
            assert body["byte_size"] > 0
            assert len(body["sha256"]) == 64
            assert body["expires_seconds"] == 900
            assert body["cached"] is False
            assert body["download_url"].startswith("https://minio.local/")
            mock_storage.upload_file.assert_called_once()
        finally:
            app.dependency_overrides.clear()

    def test_returns_404_when_layer_missing_and_422_on_invalid_format(self) -> None:
        missing_id = uuid.uuid4()
        try:
            session = _mock_session_with_parcels(None, [])
            app.dependency_overrides[get_async_session] = lambda: session
            with TestClient(app) as client:
                r1 = client.get(f"/api/v1/layers/{missing_id}/export?format=geojson")
                r2 = client.get(f"/api/v1/layers/{missing_id}/export?format=shapefile")
            assert r1.status_code == 404
            assert r2.status_code in (400, 422)
        finally:
            app.dependency_overrides.clear()
