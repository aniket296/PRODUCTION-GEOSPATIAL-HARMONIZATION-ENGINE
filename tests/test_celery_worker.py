"""Tests for Celery application, task queues, routing, and task dispatch."""

import uuid
import pytest
from celery import Celery

from backend.worker.celery_app import (
    auto_heal_topology_task,
    celery_app,
    detect_topology_errors_task,
    export_harmonized_layer_task,
    process_cadastral_layer_task,
)


def test_celery_app_initialization():
    assert celery_app.main == "cadastral_harmonization_engine"
    assert celery_app.conf.task_serializer == "json"
    assert "json" in celery_app.conf.accept_content
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.enable_utc is True
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_time_limit == 3600
    assert celery_app.conf.task_soft_time_limit == 3300


def test_celery_task_registration():
    registered = celery_app.tasks.keys()
    assert "backend.worker.celery_app.process_cadastral_layer_task" in registered
    assert "backend.worker.celery_app.detect_topology_errors_task" in registered
    assert "backend.worker.celery_app.auto_heal_topology_task" in registered
    assert "backend.worker.celery_app.export_harmonized_layer_task" in registered


def test_celery_queues_and_routing():
    queue_names = {q.name for q in celery_app.conf.task_queues}
    assert "geo_ingestion" in queue_names
    assert "geo_harmonization" in queue_names
    assert "geo_export" in queue_names

    routes = celery_app.conf.task_routes
    assert routes["backend.worker.celery_app.process_cadastral_layer_task"]["queue"] == "geo_ingestion"
    assert routes["backend.worker.celery_app.detect_topology_errors_task"]["queue"] == "geo_harmonization"
    assert routes["backend.worker.celery_app.auto_heal_topology_task"]["queue"] == "geo_harmonization"
    assert routes["backend.worker.celery_app.export_harmonized_layer_task"]["queue"] == "geo_export"


def test_celery_task_dispatch_eager_execution():
    # Configure deterministic synchronous eager execution for unit test
    original_backend = celery_app.conf.result_backend
    celery_app.conf.result_backend = "cache+memory://"
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True

    try:
        layer_id = str(uuid.uuid4())

        # 1. Process cadastral layer task
        res_ingest = process_cadastral_layer_task.delay(layer_id=layer_id)
        assert res_ingest.successful()
        val_ingest = res_ingest.get()
        assert val_ingest["layer_id"] == layer_id
        assert val_ingest["action"] == "ingest_and_validate"

        # 2. Detect topology errors task
        res_topo = detect_topology_errors_task.delay(layer_id=layer_id)
        assert res_topo.successful()
        val_topo = res_topo.get()
        assert val_topo["layer_id"] == layer_id
        assert val_topo["action"] == "detect_topology_errors"

        # 3. Auto heal topology task
        res_heal = auto_heal_topology_task.delay(layer_id=layer_id)
        assert res_heal.successful()
        val_heal = res_heal.get()
        assert val_heal["layer_id"] == layer_id
        assert val_heal["action"] == "auto_heal_topology"

        # 4. Export harmonized layer task
        res_export = export_harmonized_layer_task.delay(layer_id=layer_id, export_format="FlatGeobuf")
        assert res_export.successful()
        val_export = res_export.get()
        assert val_export["layer_id"] == layer_id
        assert val_export["export_format"] == "FlatGeobuf"
        assert val_export["action"] == "export_harmonized_layer"

    finally:
        celery_app.conf.result_backend = original_backend
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


def test_dead_letter_queue_and_ping_task():
    from backend.worker.celery_app import celery_ping_task

    queue_names = {q.name for q in celery_app.conf.task_queues}
    assert "geo_dead_letter" in queue_names

    res = celery_ping_task()
    assert res["status"] == "PONG"
    assert res["service"] == "cadastral_harmonization_engine"


def test_get_celery_app_dependency():
    from backend.worker.celery_app import get_celery_app

    app = get_celery_app()
    assert isinstance(app, Celery)
    assert app is celery_app


def test_cadastre_task_base_progress_update():
    from unittest.mock import MagicMock
    from backend.worker.celery_app import CadastreTask

    task = CadastreTask()
    task.update_state = MagicMock()

    task.update_progress(45, "RESOLVING_SLIVERS", {"count": 12})
    task.update_state.assert_called_once_with(
        state="PROGRESS",
        meta={
            "percent": 45,
            "stage": "RESOLVING_SLIVERS",
            "details": {"count": 12},
        },
    )


def test_task_retry_configuration():
    assert Exception in process_cadastral_layer_task.autoretry_for
    assert process_cadastral_layer_task.max_retries == 3
    assert process_cadastral_layer_task.retry_backoff is True


def test_process_cadastral_layer_task_vector_with_normalization(tmp_path):
    import json
    from backend.worker.ingestion_state import IngestionState

    geojson_file = tmp_path / "cadastral_sample.geojson"
    geojson_data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "plot_101",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[77.10, 28.50], [77.12, 28.50], [77.12, 28.52], [77.10, 28.52], [77.10, 28.50]]
                    ],
                },
                "properties": {"khasra_no": "101", "village": "Rampur"},
            },
            {
                "type": "Feature",
                "id": "plot_102",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[77.12, 28.50], [77.14, 28.50], [77.14, 28.52], [77.12, 28.52], [77.12, 28.50]]
                    ],
                },
                "properties": {"khasra_no": "102", "village": "Rampur"},
            },
        ],
    }
    geojson_file.write_text(json.dumps(geojson_data), encoding="utf-8")

    layer_id = str(uuid.uuid4())
    res = process_cadastral_layer_task.apply(
        args=[layer_id],
        kwargs={"file_path": str(geojson_file), "normalize_geometries": True},
    )

    assert res.successful()
    result = res.result
    assert result["layer_id"] == layer_id
    assert result["status"] == "COMPLETED"
    assert result["feature_count"] == 2
    assert result["normalized_count"] == 2
    assert result["is_raster"] is False
    assert result["tracker"]["state"] == IngestionState.COMPLETED.value
    stages = [h["to"] for h in result["tracker"]["history"]]
    assert IngestionState.VALIDATING_FORMAT.value in stages
    assert IngestionState.INGESTING_FEATURES.value in stages
    assert IngestionState.NORMALIZING_GEOMETRY.value in stages
    assert stages[-1] == IngestionState.COMPLETED.value


def test_process_cadastral_layer_task_vector_skip_normalization(tmp_path):
    import json
    from backend.worker.ingestion_state import IngestionState

    geojson_file = tmp_path / "cadastral_raw.geojson"
    geojson_data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "plot_201",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[77.20, 28.60], [77.21, 28.60], [77.21, 28.61], [77.20, 28.61], [77.20, 28.60]]
                    ],
                },
                "properties": {"khasra_no": "201"},
            }
        ],
    }
    geojson_file.write_text(json.dumps(geojson_data), encoding="utf-8")

    layer_id = str(uuid.uuid4())
    res = process_cadastral_layer_task.apply(
        args=[layer_id],
        kwargs={"file_path": str(geojson_file), "normalize_geometries": False},
    )

    assert res.successful()
    result = res.result
    assert result["status"] == "COMPLETED"
    stages = [h["to"] for h in result["tracker"]["history"]]
    assert IngestionState.NORMALIZING_GEOMETRY.value not in stages
    assert stages[-1] == IngestionState.COMPLETED.value


def test_process_cadastral_layer_task_invalid_payload_fails(tmp_path):
    corrupt_file = tmp_path / "corrupt.geojson"
    corrupt_file.write_bytes(b"NOT_A_GEOJSON_OR_GEOSPATIAL_FILE")

    layer_id = str(uuid.uuid4())
    res = process_cadastral_layer_task.apply(
        args=[layer_id],
        kwargs={"file_path": str(corrupt_file)},
    )
    assert res.status == "FAILURE"
    assert isinstance(res.result, ValueError)
    assert "Upload validation failed" in str(res.result)


def test_upload_validator_validate_file_helper(tmp_path):
    from backend.ingestion.validation import UploadValidator

    # 1. Non-existent file
    missing = tmp_path / "missing.shp"
    rep_missing = UploadValidator.validate_file(missing)
    assert not rep_missing.is_valid
    assert rep_missing.error_code == "FILE_NOT_FOUND"

    # 2. Valid file
    valid = tmp_path / "valid.geojson"
    valid.write_text('{"type": "FeatureCollection", "features": []}', encoding="utf-8")
    rep_valid = UploadValidator.validate_file(valid)
    assert rep_valid.is_valid

