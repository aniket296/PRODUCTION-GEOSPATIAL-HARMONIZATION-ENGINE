"""Tests for Cadastral Layer Upload API endpoint (POST /api/v1/layers/upload)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
import shapefile

from backend.api.app import app
from backend.core.config import get_settings
from backend.db.session import get_async_session
from backend.worker.celery_app import celery_app, process_cadastral_layer_task


@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch):
    """Auto-use fixture: Mock DB session and Celery broker connection for offline testing."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    async def _dummy_session():
        yield mock_session

    app.dependency_overrides[get_async_session] = _dummy_session

    # Configure eager memory backend for celery
    original_broker = celery_app.conf.broker_url
    original_backend = celery_app.conf.result_backend
    original_eager = celery_app.conf.task_always_eager

    celery_app.conf.broker_url = "memory://"
    celery_app.conf.result_backend = "cache+memory://"
    celery_app.conf.task_always_eager = True

    # Default delay mock returning deterministic task id
    generated_job_id = f"job-{uuid.uuid4()}"
    mock_async_result = MagicMock()
    mock_async_result.id = generated_job_id
    monkeypatch.setattr(process_cadastral_layer_task, "delay", MagicMock(return_value=mock_async_result))
    # Mock MinIO storage upload by default for offline fast test execution
    from backend.storage.minio_client import MinioStorageClient
    monkeypatch.setattr(MinioStorageClient, "upload_file", MagicMock(return_value="s3://cadastral-raw/mock-path"))

    yield

    app.dependency_overrides.clear()
    celery_app.conf.broker_url = original_broker
    celery_app.conf.result_backend = original_backend
    celery_app.conf.task_always_eager = original_eager


@pytest.fixture
def client():
    """FastAPI test client fixture."""
    return TestClient(app)


@pytest.fixture
def sample_geojson_bytes() -> bytes:
    """Deterministic valid GeoJSON Cadastral parcel collection."""
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "parcel_101",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [77.200, 28.500],
                            [77.205, 28.500],
                            [77.205, 28.505],
                            [77.200, 28.505],
                            [77.200, 28.500],
                        ]
                    ],
                },
                "properties": {
                    "khasra_no": "101/A",
                    "village": "Badarpur",
                    "area_sqm": 2500.0,
                },
            }
        ],
    }
    return json.dumps(data).encode("utf-8")


@pytest.fixture
def sample_shapefile_zip_bytes(tmp_path) -> bytes:
    """Generate in-memory zip containing a complete valid Shapefile."""
    shp_base = str(tmp_path / "test_parcels")
    with shapefile.Writer(shp_base, shapeType=shapefile.POLYGON) as w:
        w.field("khasra_no", "C", size=50)
        w.field("village", "C", size=50)
        coords = [
            [77.10, 28.60],
            [77.11, 28.60],
            [77.11, 28.61],
            [77.10, 28.61],
            [77.10, 28.60],
        ]
        w.poly([coords])
        w.record(khasra_no="55", village="Mehrauli")

    prj_content = (
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
    )
    (tmp_path / "test_parcels.prj").write_text(prj_content, encoding="utf-8")
    (tmp_path / "test_parcels.cpg").write_text("UTF-8", encoding="utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            p = tmp_path / f"test_parcels{ext}"
            zf.write(p, arcname=f"test_parcels{ext}")

    return buf.getvalue()


def test_health_endpoints(client):
    """Verify application health probes return 200 OK."""
    res1 = client.get("/health")
    assert res1.status_code == 200
    assert res1.json()["status"] == "healthy"

    res2 = client.get("/api/v1/health")
    assert res2.status_code == 200
    assert res2.json()["status"] == "healthy"


def test_upload_geojson_returns_http_202_and_job_identifier(client, sample_geojson_bytes):
    """Verify valid GeoJSON upload returns HTTP 202 Accepted and asynchronous job ID."""
    files = {
        "file": ("cadastre_sample.geojson", sample_geojson_bytes, "application/geo+json")
    }
    response = client.post("/api/v1/layers/upload", files=files)

    assert response.status_code == 202
    data = response.json()

    # Validate essential contract fields
    assert "layer_id" in data
    assert uuid.UUID(data["layer_id"])  # Valid UUID
    assert "job_id" in data
    assert len(data["job_id"]) > 0
    assert data["status"] == "PENDING"
    assert data["filename"] == "cadastre_sample.geojson"
    assert data["file_size_bytes"] == len(sample_geojson_bytes)
    assert data["target_crs"] == "EPSG:4326"
    assert "created_at" in data

    # Verify REST standard Location header
    assert "location" in response.headers
    assert response.headers["location"] == f"/api/v1/layers/{data['layer_id']}/status"


def test_upload_shapefile_zip_returns_http_202(client, sample_shapefile_zip_bytes):
    """Verify zipped Shapefile upload is accepted and scheduled."""
    files = {
        "file": ("parcels.zip", sample_shapefile_zip_bytes, "application/zip")
    }
    response = client.post("/api/v1/layers/upload", files=files)

    assert response.status_code == 202
    data = response.json()
    assert uuid.UUID(data["layer_id"])
    assert len(data["job_id"]) > 0
    assert data["filename"] == "parcels.zip"


def test_upload_empty_payload_rejected_http_400(client):
    """Verify zero-byte upload is rejected immediately with HTTP 400 Bad Request."""
    files = {
        "file": ("empty.geojson", b"", "application/geo+json")
    }
    response = client.post("/api/v1/layers/upload", files=files)

    assert response.status_code == 400
    assert "0 bytes" in response.json()["detail"]


def test_upload_missing_file_rejected_http_422(client):
    """Verify request missing multipart 'file' field is rejected with HTTP 422."""
    response = client.post("/api/v1/layers/upload", data={"layer_name": "Test"})
    assert response.status_code == 422


def test_upload_executable_payload_rejected_http_400(client):
    """Verify malicious / executable payloads disguised with geo extension are blocked."""
    fake_exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100
    files = {
        "file": ("exploit.geojson", fake_exe, "application/geo+json")
    }
    response = client.post("/api/v1/layers/upload", files=files)

    assert response.status_code == 400
    assert "rejected" in response.json()["detail"].lower()


def test_upload_dispatches_celery_task_with_expected_args(client, sample_geojson_bytes, monkeypatch):
    """Verify upload handler schedules Celery task with expected spatial parameters."""
    mock_delay = MagicMock()
    mock_delay.return_value.id = "celery-task-uuid-999"
    monkeypatch.setattr(process_cadastral_layer_task, "delay", mock_delay)

    files = {
        "file": ("sector_5.geojson", sample_geojson_bytes, "application/geo+json")
    }
    data_form = {
        "layer_name": "Sector 5 Revenue Survey",
        "declared_crs": "EPSG:4326",
        "target_crs": "EPSG:32643",
        "normalize_geometries": "true",
    }
    response = client.post("/api/v1/layers/upload", files=files, data=data_form)

    assert response.status_code == 202
    res_data = response.json()
    assert res_data["job_id"] == "celery-task-uuid-999"
    assert res_data["declared_crs"] == "EPSG:4326"
    assert res_data["target_crs"] == "EPSG:32643"

    mock_delay.assert_called_once()
    call_kwargs = mock_delay.call_args.kwargs
    assert call_kwargs["layer_id"] == res_data["layer_id"]
    assert "sector_5.geojson" in call_kwargs["file_path"]
    assert call_kwargs["target_crs"] == "EPSG:32643"
    assert call_kwargs["normalize_geometries"] is True


def test_upload_oversized_payload_rejected_http_413(client, monkeypatch):
    """Verify payloads exceeding configured MAX_UPLOAD_SIZE_BYTES are rejected with 413."""
    from backend.core.config import Settings
    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_size_bytes", 50)

    oversized_data = b'{"type": "FeatureCollection", "features": []}' * 10  # > 50 bytes
    files = {
        "file": ("big.geojson", oversized_data, "application/geo+json")
    }
    response = client.post("/api/v1/layers/upload", files=files)

    assert response.status_code == 413
    assert "exceeds maximum limit" in response.json()["detail"]


def test_upload_persists_raw_layer_in_db(client, sample_geojson_bytes):
    """Verify RawCadastralLayer model is populated with upload metadata and added to DB session."""
    from backend.db.session import get_async_session
    from backend.models.layer import RawCadastralLayer

    persisted_layers = []
    mock_session = AsyncMock()
    mock_session.add = MagicMock(side_effect=lambda layer: persisted_layers.append(layer))
    mock_session.commit = AsyncMock()

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        files = {
            "file": ("revenue_map.geojson", sample_geojson_bytes, "application/geo+json")
        }
        response = client.post(
            "/api/v1/layers/upload",
            files=files,
            data={"layer_name": "Revenue Map 2026", "declared_crs": "EPSG:4326"},
        )
        assert response.status_code == 202
        assert len(persisted_layers) == 1
        layer = persisted_layers[0]
        assert isinstance(layer, RawCadastralLayer)
        assert layer.name == "Revenue Map 2026"
        assert layer.filename == "revenue_map.geojson"
        assert layer.file_format == "GEOJSON"
        assert layer.source_crs == "EPSG:4326"
        assert layer.metadata_json["file_size_bytes"] == len(sample_geojson_bytes)
        assert layer.metadata_json["detected_format"] == "GEOJSON"
        mock_session.commit.assert_awaited_once()
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_upload_minio_storage_upload_attempt(client, sample_geojson_bytes, monkeypatch):
    """Verify upload handler attempts to store raw asset in MinIO S3 bucket."""
    from backend.storage.minio_client import MinioStorageClient

    mock_upload = MagicMock(return_value="s3://cadastral-raw/mock-path")
    monkeypatch.setattr(MinioStorageClient, "upload_file", mock_upload)

    files = {
        "file": ("survey.geojson", sample_geojson_bytes, "application/geo+json")
    }
    response = client.post("/api/v1/layers/upload", files=files)
    assert response.status_code == 202

    mock_upload.assert_called_once()
    call_kwargs = mock_upload.call_args.kwargs
    assert call_kwargs["bucket_name"] == "cadastral-raw"
    assert "survey.geojson" in call_kwargs["object_name"]


def test_upload_corrupt_zip_rejected_http_400(client):
    """Verify corrupt zip archive upload is rejected with HTTP 400 Bad Request."""
    corrupt_zip = b"PK\x03\x04" + b"CORRUPT_BYTES_NOT_A_ZIP" * 5
    files = {
        "file": ("corrupt.zip", corrupt_zip, "application/zip")
    }
    response = client.post("/api/v1/layers/upload", files=files)
    assert response.status_code == 400
    assert "rejected" in response.json()["detail"].lower()

