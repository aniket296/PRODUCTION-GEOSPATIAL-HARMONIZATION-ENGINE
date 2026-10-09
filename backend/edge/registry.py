"""Edge worker registry managing decentralized cadastral survey computing nodes."""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from backend.edge.models import (
    EdgeWorker,
    HardwareMetrics,
    HeartbeatPayload,
    WorkerCapability,
    WorkerNotFoundError,
    WorkerRegistrationError,
    WorkerStatus,
)


_IP_V4_REGEX = re.compile(
    r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
)


class EdgeWorkerRegistry:
    """Thread-safe catalog and lease manager for distributed edge processing nodes."""

    def __init__(self) -> None:
        self._workers: Dict[str, EdgeWorker] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        """Reset all registered workers (useful for test isolation)."""
        with self._lock:
            self._workers.clear()

    @staticmethod
    def _validate_registration_params(
        name: str,
        district: str,
        state: str,
        ip_address: str,
        port: int,
    ) -> None:
        """Strict validation of worker registration attributes."""
        if not name or not name.strip():
            raise WorkerRegistrationError("Worker name cannot be empty.")
        if not district or not district.strip():
            raise WorkerRegistrationError("Administrative district cannot be empty.")
        if not state or not state.strip():
            raise WorkerRegistrationError("State cannot be empty.")
        if not ip_address or (
            ip_address != "localhost"
            and not _IP_V4_REGEX.match(ip_address)
            and not ip_address.startswith("127.")
        ):
            raise WorkerRegistrationError(f"Invalid IP address format: '{ip_address}'")
        if port < 1 or port > 65535:
            raise WorkerRegistrationError(f"Port must be between 1 and 65535, got {port}")

    def register_worker(
        self,
        name: str,
        district: str,
        state: str,
        ip_address: str,
        port: int,
        capabilities: Optional[Sequence[WorkerCapability]] = None,
        tags: Optional[Dict[str, str]] = None,
        heartbeat_timeout_sec: float = 30.0,
        worker_id: Optional[str] = None,
    ) -> EdgeWorker:
        """Register a new edge worker or update an existing registration lease."""
        self._validate_registration_params(name, district, state, ip_address, port)

        now = datetime.now(timezone.utc)
        clean_district = district.strip().lower().replace(" ", "-")

        with self._lock:
            if not worker_id:
                worker_id = f"edge-{clean_district}-{uuid.uuid4().hex[:8]}"

            caps: Set[WorkerCapability] = set(capabilities or [])
            worker_tags = dict(tags or {})

            # Check if updating an existing worker
            if worker_id in self._workers:
                worker = self._workers[worker_id]
                worker.name = name.strip()
                worker.district = district.strip()
                worker.state = state.strip()
                worker.ip_address = ip_address
                worker.port = port
                worker.capabilities = caps
                worker.tags = worker_tags
                worker.status = WorkerStatus.ONLINE
                worker.last_heartbeat_at = now
                worker.heartbeat_timeout_sec = heartbeat_timeout_sec
                worker.consecutive_missed_heartbeats = 0
                return worker

            new_worker = EdgeWorker(
                worker_id=worker_id,
                name=name.strip(),
                district=district.strip(),
                state=state.strip(),
                ip_address=ip_address,
                port=port,
                capabilities=caps,
                status=WorkerStatus.ONLINE,
                registered_at=now,
                last_heartbeat_at=now,
                heartbeat_timeout_sec=heartbeat_timeout_sec,
                tags=worker_tags,
                consecutive_missed_heartbeats=0,
            )
            self._workers[worker_id] = new_worker
            return new_worker

    def get_worker(self, worker_id: str) -> Optional[EdgeWorker]:
        """Lookup an edge worker by its unique identifier."""
        with self._lock:
            return self._workers.get(worker_id)

    def get_worker_or_raise(self, worker_id: str) -> EdgeWorker:
        """Lookup an edge worker, raising WorkerNotFoundError if not found."""
        worker = self.get_worker(worker_id)
        if not worker:
            raise WorkerNotFoundError(f"Edge worker '{worker_id}' is not registered.")
        return worker

    def list_workers(
        self,
        status: Optional[WorkerStatus] = None,
        district: Optional[str] = None,
        state: Optional[str] = None,
        capability: Optional[WorkerCapability] = None,
    ) -> List[EdgeWorker]:
        """List registered workers matching optional filter criteria."""
        with self._lock:
            results = list(self._workers.values())

        if status:
            results = [w for w in results if w.status == status]
        if district:
            d_lower = district.strip().lower()
            results = [w for w in results if w.district.lower() == d_lower]
        if state:
            s_lower = state.strip().lower()
            results = [w for w in results if w.state.lower() == s_lower]
        if capability:
            results = [w for w in results if capability in w.capabilities]

        return results

    def record_heartbeat(
        self,
        worker_id: str,
        payload: Optional[HeartbeatPayload] = None,
    ) -> EdgeWorker:
        """Process an incoming heartbeat transmission from a registered edge worker."""
        with self._lock:
            worker = self.get_worker_or_raise(worker_id)

            now = datetime.now(timezone.utc)
            worker.last_heartbeat_at = now
            worker.consecutive_missed_heartbeats = 0

            # If worker was offline or degraded, transition back to active
            if worker.status in (WorkerStatus.OFFLINE, WorkerStatus.DEGRADED):
                worker.status = WorkerStatus.ONLINE

            if payload:
                if payload.status:
                    worker.status = payload.status
                if payload.metrics:
                    worker.metrics = payload.metrics

            return worker

    def set_worker_status(self, worker_id: str, status: WorkerStatus) -> EdgeWorker:
        """Explicitly update the lifecycle status of a worker."""
        with self._lock:
            worker = self.get_worker_or_raise(worker_id)
            worker.status = status
            return worker

    def deregister_worker(self, worker_id: str, reason: str = "graceful_shutdown") -> bool:
        """Deregister an edge worker from the active pool."""
        with self._lock:
            worker = self.get_worker(worker_id)
            if not worker:
                return False
            worker.status = WorkerStatus.DEREGISTERED
            worker.tags["deregistration_reason"] = reason
            del self._workers[worker_id]
            return True

    def count(self) -> int:
        """Return total number of currently registered workers."""
        with self._lock:
            return len(self._workers)

    def find_eligible_workers(
        self,
        required_capability: Optional[WorkerCapability] = None,
        district: Optional[str] = None,
        max_cpu_pct: float = 85.0,
    ) -> List[EdgeWorker]:
        """Find active workers capable of handling tasks, ordered by lowest CPU load and task count."""
        candidates = self.list_workers(district=district, capability=required_capability)
        # Must be alive
        candidates = [w for w in candidates if w.is_alive]

        # Filter out overloaded workers
        if max_cpu_pct > 0.0:
            candidates = [
                w
                for w in candidates
                if w.metrics is None or w.metrics.cpu_usage_pct <= max_cpu_pct
            ]

        # Sort: prefer IDLE over BUSY, then lowest CPU usage, then least active tasks
        def _sort_key(w: EdgeWorker) -> Tuple[int, float, int]:
            status_priority = 0 if w.status == WorkerStatus.IDLE else (1 if w.status == WorkerStatus.ONLINE else 2)
            cpu = w.metrics.cpu_usage_pct if w.metrics else 50.0
            tasks = w.metrics.active_tasks if w.metrics else 0
            return (status_priority, cpu, tasks)

        return sorted(candidates, key=_sort_key)


# Global singleton instance
edge_worker_registry = EdgeWorkerRegistry()
