"""Ingestion lifecycle state machine for asynchronous cadastral layer processing."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional

from backend.models.layer import LayerStatus


class IngestionState(str, enum.Enum):
    """Fine-grained stages a vector/raster ingestion job passes through."""

    QUEUED = "QUEUED"
    VALIDATING_FORMAT = "VALIDATING_FORMAT"
    INGESTING_FEATURES = "INGESTING_FEATURES"
    NORMALIZING_GEOMETRY = "NORMALIZING_GEOMETRY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: FrozenSet[IngestionState] = frozenset(
    {IngestionState.COMPLETED, IngestionState.FAILED, IngestionState.CANCELLED}
)

# Allowed forward transitions. Any non-terminal state may additionally fail or be cancelled.
_FORWARD: Dict[IngestionState, FrozenSet[IngestionState]] = {
    IngestionState.QUEUED: frozenset({IngestionState.VALIDATING_FORMAT}),
    IngestionState.VALIDATING_FORMAT: frozenset({IngestionState.INGESTING_FEATURES}),
    IngestionState.INGESTING_FEATURES: frozenset({IngestionState.NORMALIZING_GEOMETRY, IngestionState.COMPLETED}),
    IngestionState.NORMALIZING_GEOMETRY: frozenset({IngestionState.COMPLETED}),
    IngestionState.COMPLETED: frozenset(),
    IngestionState.FAILED: frozenset(),
    IngestionState.CANCELLED: frozenset(),
}

# Progress percentage reported to clients for each stage.
STATE_PROGRESS: Dict[IngestionState, int] = {
    IngestionState.QUEUED: 0,
    IngestionState.VALIDATING_FORMAT: 10,
    IngestionState.INGESTING_FEATURES: 30,
    IngestionState.NORMALIZING_GEOMETRY: 70,
    IngestionState.COMPLETED: 100,
    IngestionState.FAILED: 100,
    IngestionState.CANCELLED: 100,
}

# Mapping onto the persisted RawCadastralLayer.status values.
_LAYER_STATUS: Dict[IngestionState, LayerStatus] = {
    IngestionState.QUEUED: LayerStatus.PENDING,
    IngestionState.VALIDATING_FORMAT: LayerStatus.PROCESSING,
    IngestionState.INGESTING_FEATURES: LayerStatus.PROCESSING,
    IngestionState.NORMALIZING_GEOMETRY: LayerStatus.PROCESSING,
    IngestionState.COMPLETED: LayerStatus.COMPLETED,
    IngestionState.FAILED: LayerStatus.FAILED,
    IngestionState.CANCELLED: LayerStatus.FAILED,
}


class InvalidStateTransitionError(RuntimeError):
    """Raised when an ingestion job attempts an illegal lifecycle transition."""


def is_terminal(state: IngestionState) -> bool:
    """Return True when no further transitions are possible from ``state``."""
    return state in TERMINAL_STATES


def allowed_transitions(state: IngestionState) -> FrozenSet[IngestionState]:
    """Return every state reachable in one step from ``state``."""
    if is_terminal(state):
        return frozenset()
    return _FORWARD[state] | {IngestionState.FAILED, IngestionState.CANCELLED}


def can_transition(current: IngestionState, target: IngestionState) -> bool:
    """Return True when moving from ``current`` to ``target`` is legal."""
    return target in allowed_transitions(current)


def to_layer_status(state: IngestionState) -> LayerStatus:
    """Map an ingestion stage to its persisted layer status."""
    return _LAYER_STATUS[state]


@dataclass
class StateTransition:
    """Single recorded lifecycle transition."""

    from_state: Optional[IngestionState]
    to_state: IngestionState
    timestamp: datetime
    message: Optional[str] = None


@dataclass
class IngestionStateTracker:
    """Tracks and validates the lifecycle of one ingestion job."""

    layer_id: str
    state: IngestionState = IngestionState.QUEUED
    error: Optional[str] = None
    history: List[StateTransition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(
                StateTransition(None, self.state, datetime.now(timezone.utc), "Job created")
            )

    @property
    def progress(self) -> int:
        return STATE_PROGRESS[self.state]

    @property
    def layer_status(self) -> LayerStatus:
        return to_layer_status(self.state)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    def transition(self, target: IngestionState, message: Optional[str] = None) -> StateTransition:
        """Advance to ``target`` or raise :class:`InvalidStateTransitionError`."""
        if not can_transition(self.state, target):
            raise InvalidStateTransitionError(
                f"Illegal ingestion transition {self.state.value} -> {target.value} for layer {self.layer_id}"
            )
        record = StateTransition(self.state, target, datetime.now(timezone.utc), message)
        self.state = target
        self.history.append(record)
        return record

    def fail(self, error_message: str) -> StateTransition:
        """Move to FAILED and record the error."""
        record = self.transition(IngestionState.FAILED, error_message)
        self.error = error_message
        return record

    def cancel(self, message: Optional[str] = None) -> StateTransition:
        """Move to CANCELLED."""
        return self.transition(IngestionState.CANCELLED, message or "Cancelled by user")

    def to_dict(self) -> Dict[str, object]:
        """Serialize current status for API / Celery meta payloads."""
        return {
            "layer_id": self.layer_id,
            "state": self.state.value,
            "layer_status": self.layer_status.value,
            "progress": self.progress,
            "error": self.error,
            "history": [
                {
                    "from": t.from_state.value if t.from_state else None,
                    "to": t.to_state.value,
                    "timestamp": t.timestamp.isoformat(),
                    "message": t.message,
                }
                for t in self.history
            ],
        }
