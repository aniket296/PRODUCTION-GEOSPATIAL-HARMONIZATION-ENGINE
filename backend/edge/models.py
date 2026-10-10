"""Domain models and data structures for edge worker orchestration.

Represents decentralized survey field office compute nodes, hardware telemetry,
registration leases, and heartbeat packets.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set


class WorkerStatus(str, enum.Enum):
    """Lifecycle status of a distributed edge compute worker."""

    ONLINE = "ONLINE"
    IDLE = "IDLE"
    BUSY = "BUSY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    DRAINING = "DRAINING"
    DEREGISTERED = "DEREGISTERED"


class WorkerCapability(str, enum.Enum):
    """Geospatial processing capabilities advertised by an edge worker."""

    CADASTRAL_INGESTION = "CADASTRAL_INGESTION"
    TOPOLOGY_CLEANING = "TOPOLOGY_CLEANING"
    GEODETIC_TRANSFORMATION = "GEODETIC_TRANSFORMATION"
    ORTHORECTIFICATION = "ORTHORECTIFICATION"
    OFFLINE_SYNC = "OFFLINE_SYNC"


class EdgeWorkerError(Exception):
    """Base exception for edge worker operations."""

    pass


class WorkerNotFoundError(EdgeWorkerError):
    """Raised when an edge worker is not found in the registry."""

    pass


class WorkerRegistrationError(EdgeWorkerError):
    """Raised when worker registration fails due to invalid parameters or state."""

    pass


@dataclass
class HardwareMetrics:
    """Hardware health and performance telemetry reported by an edge worker."""

    cpu_usage_pct: float
    memory_usage_pct: float
    memory_available_mb: float
    disk_free_gb: float
    active_tasks: int = 0
    completed_tasks: int = 0
    uptime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cpu_usage_pct": round(self.cpu_usage_pct, 2),
            "memory_usage_pct": round(self.memory_usage_pct, 2),
            "memory_available_mb": round(self.memory_available_mb, 1),
            "disk_free_gb": round(self.disk_free_gb, 2),
            "active_tasks": self.active_tasks,
            "completed_tasks": self.completed_tasks,
            "uptime_seconds": round(self.uptime_seconds, 1),
        }


@dataclass
class EdgeWorker:
    """Registered edge worker node representing a local cadastral office compute instance."""

    worker_id: str
    name: str
    district: str
    state: str
    ip_address: str
    port: int
    capabilities: Set[WorkerCapability] = field(default_factory=set)
    status: WorkerStatus = WorkerStatus.ONLINE
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    heartbeat_timeout_sec: float = 30.0
    metrics: Optional[HardwareMetrics] = None
    tags: Dict[str, str] = field(default_factory=dict)
    consecutive_missed_heartbeats: int = 0
    auth_token: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def is_alive(self) -> bool:
        """Check if worker is currently in an active, responsive state."""
        return self.status in (WorkerStatus.ONLINE, WorkerStatus.IDLE, WorkerStatus.BUSY)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "name": self.name,
            "district": self.district,
            "state": self.state,
            "ip_address": self.ip_address,
            "port": self.port,
            "capabilities": [c.value for c in self.capabilities],
            "status": self.status.value,
            "registered_at": self.registered_at.isoformat(),
            "last_heartbeat_at": self.last_heartbeat_at.isoformat(),
            "heartbeat_timeout_sec": self.heartbeat_timeout_sec,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "tags": dict(self.tags),
            "consecutive_missed_heartbeats": self.consecutive_missed_heartbeats,
            "auth_token": self.auth_token,
        }


@dataclass
class HeartbeatPayload:
    """Heartbeat packet transmitted periodically by an active edge worker."""

    worker_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: WorkerStatus = WorkerStatus.ONLINE
    metrics: Optional[HardwareMetrics] = None
    active_task_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "active_task_ids": list(self.active_task_ids),
        }

