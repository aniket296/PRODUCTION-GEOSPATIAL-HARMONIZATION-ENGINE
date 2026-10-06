"""Tests for Celery ingestion state transitions."""

import json
import uuid
from pathlib import Path

import pytest

from backend.models.layer import LayerStatus
from backend.worker.celery_app import CadastreTask, celery_app, process_cadastral_layer_task
from backend.worker.ingestion_state import (
    STATE_PROGRESS,
    TERMINAL_STATES,
    IngestionState,
    IngestionStateTracker,
    InvalidStateTransitionError,
    allowed_transitions,
    can_transition,
    is_terminal,
    to_layer_status,
)

HAPPY_PATH = [
    IngestionState.QUEUED,
    IngestionState.VALIDATING_FORMAT,
    IngestionState.INGESTING_FEATURES,
    IngestionState.NORMALIZING_GEOMETRY,
    IngestionState.COMPLETED,
]


def _geojson(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "P1",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[77.0, 28.0], [77.001, 28.0], [77.001, 28.001], [77.0, 28.001], [77.0, 28.0]]],
                        },
                        "properties": {"khasra_no": "1"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


# --- Transition table -------------------------------------------------------


def test_happy_path_transitions_are_legal():
    for current, nxt in zip(HAPPY_PATH, HAPPY_PATH[1:]):
        assert can_transition(current, nxt), f"{current} -> {nxt}"


def test_normalization_stage_is_optional():
    assert can_transition(IngestionState.INGESTING_FEATURES, IngestionState.COMPLETED)


@pytest.mark.parametrize(
    "current,target",
    [
        (IngestionState.QUEUED, IngestionState.INGESTING_FEATURES),
        (IngestionState.QUEUED, IngestionState.COMPLETED),
        (IngestionState.VALIDATING_FORMAT, IngestionState.NORMALIZING_GEOMETRY),
        (IngestionState.VALIDATING_FORMAT, IngestionState.COMPLETED),
        (IngestionState.INGESTING_FEATURES, IngestionState.VALIDATING_FORMAT),
        (IngestionState.NORMALIZING_GEOMETRY, IngestionState.INGESTING_FEATURES),
        (IngestionState.COMPLETED, IngestionState.QUEUED),
    ],
)
def test_skipping_or_going_backwards_is_illegal(current, target):
    assert not can_transition(current, target)


@pytest.mark.parametrize("state", [s for s in IngestionState if s not in TERMINAL_STATES])
def test_any_active_state_can_fail_or_cancel(state):
    assert can_transition(state, IngestionState.FAILED)
    assert can_transition(state, IngestionState.CANCELLED)


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_have_no_exits(state):
    assert is_terminal(state)
    assert allowed_transitions(state) == frozenset()
    for target in IngestionState:
        assert not can_transition(state, target)


def test_self_transitions_are_illegal():
    for state in IngestionState:
        assert not can_transition(state, state)


# --- Progress and layer status mapping --------------------------------------


def test_progress_is_monotonic_along_happy_path():
    values = [STATE_PROGRESS[s] for s in HAPPY_PATH]
    assert values == sorted(values)
    assert values[0] == 0 and values[-1] == 100


def test_every_state_has_progress_and_layer_status():
    for state in IngestionState:
        assert 0 <= STATE_PROGRESS[state] <= 100
        assert isinstance(to_layer_status(state), LayerStatus)


def test_layer_status_mapping():
    assert to_layer_status(IngestionState.QUEUED) == LayerStatus.PENDING
    for s in (IngestionState.VALIDATING_FORMAT, IngestionState.INGESTING_FEATURES, IngestionState.NORMALIZING_GEOMETRY):
        assert to_layer_status(s) == LayerStatus.PROCESSING
    assert to_layer_status(IngestionState.COMPLETED) == LayerStatus.COMPLETED
    assert to_layer_status(IngestionState.FAILED) == LayerStatus.FAILED
    assert to_layer_status(IngestionState.CANCELLED) == LayerStatus.FAILED


# --- Tracker ----------------------------------------------------------------


def test_tracker_starts_queued_with_creation_history():
    t = IngestionStateTracker(layer_id="L1")
    assert t.state == IngestionState.QUEUED
    assert t.progress == 0
    assert t.layer_status == LayerStatus.PENDING
    assert len(t.history) == 1 and t.history[0].from_state is None


def test_tracker_walks_full_happy_path_and_records_history():
    t = IngestionStateTracker(layer_id="L1")
    for state in HAPPY_PATH[1:]:
        t.transition(state)
    assert t.state == IngestionState.COMPLETED
    assert t.is_terminal
    assert [h.to_state for h in t.history] == HAPPY_PATH
    assert t.history[1].from_state == IngestionState.QUEUED
    assert t.error is None


def test_tracker_rejects_illegal_transition_without_mutating_state():
    t = IngestionStateTracker(layer_id="L1")
    with pytest.raises(InvalidStateTransitionError, match="QUEUED -> COMPLETED"):
        t.transition(IngestionState.COMPLETED)
    assert t.state == IngestionState.QUEUED
    assert len(t.history) == 1


def test_tracker_fail_records_error_and_is_terminal():
    t = IngestionStateTracker(layer_id="L1")
    t.transition(IngestionState.VALIDATING_FORMAT)
    t.fail("Corrupt archive")
    assert t.state == IngestionState.FAILED
    assert t.error == "Corrupt archive"
    assert t.layer_status == LayerStatus.FAILED
    with pytest.raises(InvalidStateTransitionError):
        t.transition(IngestionState.INGESTING_FEATURES)


def test_tracker_cannot_fail_twice_or_after_completion():
    t = IngestionStateTracker(layer_id="L1")
    for state in HAPPY_PATH[1:]:
        t.transition(state)
    with pytest.raises(InvalidStateTransitionError):
        t.fail("too late")
    assert t.error is None


def test_tracker_cancel_default_message():
    t = IngestionStateTracker(layer_id="L1")
    t.cancel()
    assert t.state == IngestionState.CANCELLED
    assert t.history[-1].message == "Cancelled by user"
    assert t.is_terminal


def test_tracker_to_dict_is_json_serializable():
    t = IngestionStateTracker(layer_id="L9")
    t.transition(IngestionState.VALIDATING_FORMAT, "sniffing")
    payload = t.to_dict()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["state"] == "VALIDATING_FORMAT"
    assert round_tripped["layer_status"] == "PROCESSING"
    assert round_tripped["progress"] == 10
    assert round_tripped["history"][-1]["message"] == "sniffing"
    assert round_tripped["history"][0]["from"] is None


# --- Celery task progress stages -------------------------------------------


@pytest.fixture
def eager_celery():
    original_backend = celery_app.conf.result_backend
    celery_app.conf.result_backend = "cache+memory://"
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.result_backend = original_backend
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


def test_celery_task_reports_stages_in_legal_order(tmp_path, monkeypatch, eager_celery):
    recorded = []

    def spy(self, task_id=None, state=None, meta=None, **kwargs):
        recorded.append((state, meta))

    monkeypatch.setattr(CadastreTask, "update_state", spy)

    path = _geojson(tmp_path / "layer.geojson")
    result = process_cadastral_layer_task.delay(str(uuid.uuid4()), file_path=str(path)).get()

    stages = [IngestionState(meta["stage"]) for state, meta in recorded if state == "PROGRESS"]
    assert stages[0] == IngestionState.VALIDATING_FORMAT
    assert IngestionState.INGESTING_FEATURES in stages
    assert stages[-1] == IngestionState.COMPLETED
    for current, nxt in zip(stages, stages[1:]):
        assert can_transition(current, nxt), f"{current} -> {nxt}"

    percents = [meta["percent"] for state, meta in recorded if state == "PROGRESS"]
    assert percents == sorted(percents)
    assert result["status"] == IngestionState.COMPLETED.value


def test_celery_task_without_file_reports_validating_only(monkeypatch, eager_celery):
    recorded = []
    monkeypatch.setattr(
        CadastreTask, "update_state", lambda self, task_id=None, state=None, meta=None, **kw: recorded.append(meta)
    )
    process_cadastral_layer_task.delay(str(uuid.uuid4())).get()
    assert [m["stage"] for m in recorded] == [IngestionState.VALIDATING_FORMAT.value]
