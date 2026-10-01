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

