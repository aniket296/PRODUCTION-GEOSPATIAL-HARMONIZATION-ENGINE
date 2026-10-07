"""Tests for Cadastral Layer Status API endpoint (GET /api/v1/layers/{layer_id}/status)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.db.session import get_async_session
from backend.models.layer import LayerStatus, RawCadastralLayer


@pytest.fixture
def client():
    """FastAPI test client fixture."""
    return TestClient(app)


def _mock_layer(
    layer_id: uuid.UUID,
    name: str = "Test Layer",
    status: str = LayerStatus.PENDING.value,
    feature_count: int = 0,
    bounding_box=None,
    error_message: str | None = None,
    source_crs: str | None = "EPSG:4326",
) -> RawCadastralLayer:
    """Helper creating RawCadastralLayer mock object."""
    layer = RawCadastralLayer(
        name=name,
        filename="test.geojson",
        file_format="GEOJSON",
        storage_path="/tmp/test.geojson",
        source_crs=source_crs,
        target_crs="EPSG:4326",
        status=status,
        feature_count=feature_count,
        bounding_box=bounding_box,
    )
    layer.id = layer_id
    layer.error_message = error_message
    layer.created_at = datetime.now(timezone.utc)
    layer.updated_at = datetime.now(timezone.utc)
    return layer


def test_get_layer_status_pending(client):
    """Verify layer in PENDING status reports 0% progress and QUEUED stage."""
    test_id = uuid.uuid4()
    mock_layer = _mock_layer(test_id, status=LayerStatus.PENDING.value)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()

        assert data["layer_id"] == str(test_id)
        assert data["status"] == "PENDING"
        assert data["stage"] == "QUEUED"
        assert data["progress_percent"] == 0
        assert data["feature_count"] == 0
        assert data["error_message"] is None
        assert "created_at" in data
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_processing(client):
    """Verify layer in PROCESSING status reports active stage and mid-range progress."""
    test_id = uuid.uuid4()
    mock_layer = _mock_layer(test_id, status=LayerStatus.PROCESSING.value)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()

        assert data["layer_id"] == str(test_id)
        assert data["status"] == "PROCESSING"
        assert data["stage"] == "INGESTING_FEATURES"
        assert 0 < data["progress_percent"] < 100
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_completed(client):
    """Verify layer in COMPLETED status reports 100% progress, feature count, and spatial bounds."""
    test_id = uuid.uuid4()
    bbox = {"minx": 77.10, "miny": 28.50, "maxx": 77.25, "maxy": 28.65}
    mock_layer = _mock_layer(
        test_id,
        status=LayerStatus.COMPLETED.value,
        feature_count=42,
        bounding_box=bbox,
        source_crs="EPSG:32643",
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()

        assert data["layer_id"] == str(test_id)
        assert data["status"] == "COMPLETED"
        assert data["stage"] == "COMPLETED"
        assert data["progress_percent"] == 100
        assert data["feature_count"] == 42
        assert data["detected_crs"] == "EPSG:32643"
        assert data["bounds"] == [77.10, 28.50, 77.25, 28.65]
        assert data["error_message"] is None
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_failed(client):
    """Verify layer in FAILED status reports error message and failure stage."""
    test_id = uuid.uuid4()
    mock_layer = _mock_layer(
        test_id,
        status=LayerStatus.FAILED.value,
        error_message="Corrupted Shapefile header: invalid magic byte 0x00",
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()

        assert data["layer_id"] == str(test_id)
        assert data["status"] == "FAILED"
        assert data["stage"] == "FAILED"
        assert data["progress_percent"] == 100
        assert "Corrupted Shapefile header" in data["error_message"]
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_calibrated(client):
    """Verify layer in CALIBRATED status reports 100% progress and CALIBRATED stage."""
    test_id = uuid.uuid4()
    mock_layer = _mock_layer(test_id, status=LayerStatus.CALIBRATED.value)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "CALIBRATED"
        assert data["stage"] == "CALIBRATED"
        assert data["progress_percent"] == 100
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_not_found_404(client):
    """Verify non-existent layer ID returns HTTP 404 Not Found."""
    test_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_invalid_uuid_format_422(client):
    """Verify non-UUID path parameter returns HTTP 422 Unprocessable Entity."""
    response = client.get("/api/v1/layers/invalid-uuid-string/status")
    assert response.status_code == 422


def test_get_layer_status_list_bounds_format(client):
    """Verify list-formatted bounding box is parsed cleanly into bounds array."""
    test_id = uuid.uuid4()
    bbox_list = [77.01, 28.02, 77.09, 28.08]
    mock_layer = _mock_layer(
        test_id,
        status=LayerStatus.COMPLETED.value,
        bounding_box=bbox_list,
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["bounds"] == bbox_list
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_live_celery_task_progress(client, monkeypatch):
    """Verify in-flight Celery task progress is inspected and returned in status response."""
    from backend.worker.celery_app import celery_app

    test_id = uuid.uuid4()
    job_id = "celery-job-777"
    mock_layer = _mock_layer(
        test_id,
        status=LayerStatus.PROCESSING.value,
    )
    mock_layer.metadata_json = {"job_id": job_id}

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    mock_async_res = MagicMock()
    mock_async_res.state = "PROGRESS"
    mock_async_res.info = {"percent": 75, "stage": "NORMALIZING_GEOMETRY"}
    monkeypatch.setattr(celery_app, "AsyncResult", MagicMock(return_value=mock_async_res))

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "PROCESSING"
        assert data["stage"] == "NORMALIZING_GEOMETRY"
        assert data["progress_percent"] == 75
        assert data["job_id"] == job_id
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def test_get_layer_status_total_area_and_raster_flag(client):
    """Verify total area and is_raster flag are populated accurately."""
    test_id = uuid.uuid4()
    mock_layer = _mock_layer(
        test_id,
        name="Orthomosaic 2026",
        status=LayerStatus.COMPLETED.value,
        feature_count=1,
    )
    mock_layer.file_format = "GEOTIFF"
    mock_layer.total_area_sqm = 450000.75

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_layer
    mock_session.execute.return_value = mock_result

    app.dependency_overrides[get_async_session] = lambda: mock_session

    try:
        response = client.get(f"/api/v1/layers/{test_id}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Orthomosaic 2026"
        assert data["file_format"] == "GEOTIFF"
        assert data["is_raster"] is True
        assert data["total_area_sqm"] == 450000.75
    finally:
        app.dependency_overrides.pop(get_async_session, None)


