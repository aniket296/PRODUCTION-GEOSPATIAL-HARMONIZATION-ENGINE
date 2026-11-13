"""Tests for GET /api/v1/layers/{layer_id}/flatgeobuf HTTP 206 Range Requests & bbox filtering (Step 107/150)."""

from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock

import flatgeobuf
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from starlette.testclient import TestClient

from backend.api.app import app
from backend.db.session import get_async_session
from backend.models.layer import LayerStatus, RawCadastralLayer
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
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
    dx: float,
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

    session.execute.side_effect = [layer_res, parcel_res]
    return session


def _sample_dataset(layer_id: uuid.UUID) -> tuple[RawCadastralLayer, list[HarmonizedParcel]]:
    layer = _make_layer(layer_id)
    p_west = _make_parcel(layer_id, uuid.UUID("00000000-0000-0000-0000-000000000001"), "WEST", dx=500_000.0)
    p_center = _make_parcel(layer_id, uuid.UUID("00000000-0000-0000-0000-000000000002"), "CENTER", dx=520_000.0)
    p_east = _make_parcel(layer_id, uuid.UUID("00000000-0000-0000-0000-000000000003"), "EAST", dx=540_000.0)
    return layer, [p_west, p_center, p_east]


class TestFlatGeobufStreamingEndpoint:
    def test_full_download_returns_200_with_flatgeobuf_headers_and_valid_payload(self):
        layer_id = uuid.uuid4()
        layer, parcels = _sample_dataset(layer_id)
        mock_session = _mock_session_with_parcels(layer, parcels)

        async def _override():
            yield mock_session

        app.dependency_overrides[get_async_session] = _override
        try:
            with TestClient(app) as client:
                resp = client.get(f"/api/v1/layers/{layer_id}/flatgeobuf")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.flatgeobuf")
        assert resp.headers["accept-ranges"] == "bytes"
        assert int(resp.headers["content-length"]) == len(resp.content)
        assert f"{layer_id}.fgb" in resp.headers.get("content-disposition", "")

        fc = flatgeobuf.load(io.BytesIO(resp.content))
        assert len(fc["features"]) == 3

    def test_http_206_range_requests_return_exact_byte_slices(self):
        layer_id = uuid.uuid4()
        layer, parcels = _sample_dataset(layer_id)

        def _client_call(headers=None):
            mock_session = _mock_session_with_parcels(layer, parcels)

            async def _override():
                yield mock_session

            app.dependency_overrides[get_async_session] = _override
            try:
                with TestClient(app) as client:
                    return client.get(f"/api/v1/layers/{layer_id}/flatgeobuf", headers=headers or {})
            finally:
                app.dependency_overrides.clear()

        full_resp = _client_call()
        assert full_resp.status_code == 200
        full_bytes = full_resp.content
        total = len(full_bytes)

        # Explicit closed range
        r1 = _client_call({"Range": "bytes=0-63"})
        assert r1.status_code == 206
        assert r1.headers["accept-ranges"] == "bytes"
        assert r1.headers["content-range"] == f"bytes 0-63/{total}"
        assert int(r1.headers["content-length"]) == 64
        assert r1.content == full_bytes[0:64]

        # Open-ended range
        r2 = _client_call({"Range": "bytes=64-"})
        assert r2.status_code == 206
        assert r2.headers["content-range"] == f"bytes 64-{total - 1}/{total}"
        assert r2.content == full_bytes[64:]
        assert r1.content + r2.content == full_bytes

        # Suffix range
        r3 = _client_call({"Range": "bytes=-32"})
        assert r3.status_code == 206
        assert r3.headers["content-range"] == f"bytes {total - 32}-{total - 1}/{total}"
        assert r3.content == full_bytes[-32:]

        # Clamped end beyond total
        r4 = _client_call({"Range": f"bytes=0-{total + 5000}"})
        assert r4.status_code == 206
        assert r4.headers["content-range"] == f"bytes 0-{total - 1}/{total}"
        assert r4.content == full_bytes

    def test_unsatisfiable_or_malformed_range_returns_416(self):
        layer_id = uuid.uuid4()
        layer, parcels = _sample_dataset(layer_id)

        for bad_range in ("bytes=9999999-10000000", "bytes=100-50", "bytes=abc-def"):
            mock_session = _mock_session_with_parcels(layer, parcels)

            async def _override():
                yield mock_session

            app.dependency_overrides[get_async_session] = _override
            try:
                with TestClient(app) as client:
                    resp = client.get(f"/api/v1/layers/{layer_id}/flatgeobuf", headers={"Range": bad_range})
            finally:
                app.dependency_overrides.clear()

            assert resp.status_code == 416
            assert resp.headers.get("content-range", "").startswith("bytes */")

    def test_bbox_query_filters_exported_features(self):
        layer_id = uuid.uuid4()
        layer, parcels = _sample_dataset(layer_id)
        center_bounds = project_metric_geometry_to_wgs84(
            _rect(-10.0, -10.0, 110.0, 110.0, dx=520_000.0, dy=3_000_000.0)
        ).bounds
        bbox_param = ",".join(f"{coord:.8f}" for coord in center_bounds)

        mock_session = _mock_session_with_parcels(layer, parcels)

        async def _override():
            yield mock_session

        app.dependency_overrides[get_async_session] = _override
        try:
            with TestClient(app) as client:
                resp = client.get(f"/api/v1/layers/{layer_id}/flatgeobuf?bbox={bbox_param}")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        fc = flatgeobuf.load(io.BytesIO(resp.content))
        assert len(fc["features"]) == 1
        assert fc["features"][0]["properties"]["parcel_identifier"] == "CENTER"

    def test_invalid_bbox_returns_422(self):
        layer_id = uuid.uuid4()
        with TestClient(app) as client:
            resp1 = client.get(f"/api/v1/layers/{layer_id}/flatgeobuf?bbox=10,20,30")
            resp2 = client.get(f"/api/v1/layers/{layer_id}/flatgeobuf?bbox=80,20,70,30")
        assert resp1.status_code == 422
        assert resp2.status_code == 422

    def test_missing_layer_returns_404_and_no_db_returns_503(self):
        layer_id = uuid.uuid4()
        mock_session = AsyncMock()
        layer_res = MagicMock()
        layer_res.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = layer_res

        async def _override():
            yield mock_session

        app.dependency_overrides[get_async_session] = _override
        try:
            with TestClient(app) as client:
                resp_404 = client.get(f"/api/v1/layers/{layer_id}/flatgeobuf")
        finally:
            app.dependency_overrides.clear()
        assert resp_404.status_code == 404

        async def _override_none():
            yield None

        app.dependency_overrides[get_async_session] = _override_none
        try:
            with TestClient(app) as client:
                resp_503 = client.get(f"/api/v1/layers/{layer_id}/flatgeobuf")
        finally:
            app.dependency_overrides.clear()
        assert resp_503.status_code == 503
