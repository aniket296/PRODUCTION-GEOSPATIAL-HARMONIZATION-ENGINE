"""Edge worker client for heartbeat transmission and hardware telemetry collection."""

from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.edge.models import HardwareMetrics, HeartbeatPayload, WorkerStatus
from backend.edge.registry import EdgeWorkerRegistry, edge_worker_registry


class EdgeHeartbeatClient:
    """Simulated or local agent running on an edge node that transmits heartbeats."""

    def __init__(
        self,
        worker_id: str,
        registry: Optional[EdgeWorkerRegistry] = None,
        heartbeat_interval_sec: float = 10.0,
    ) -> None:
        self.worker_id = worker_id
        self.registry = registry or edge_worker_registry
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.active_tasks: List[str] = []
        self.completed_tasks_count: int = 0
        self._start_time = time.time()

    def collect_telemetry(
        self,
        cpu_usage_pct: float = 15.0,
        memory_usage_pct: float = 35.0,
        memory_available_mb: float = 4096.0,
        disk_free_gb: float = 120.0,
    ) -> HardwareMetrics:
        """Collect local system hardware telemetry."""
        uptime = time.time() - self._start_time
        return HardwareMetrics(
            cpu_usage_pct=cpu_usage_pct,
            memory_usage_pct=memory_usage_pct,
            memory_available_mb=memory_available_mb,
            disk_free_gb=disk_free_gb,
            active_tasks=len(self.active_tasks),
            completed_tasks=self.completed_tasks_count,
            uptime_seconds=uptime,
        )

    def send_heartbeat(
        self,
        status: WorkerStatus = WorkerStatus.ONLINE,
        metrics: Optional[HardwareMetrics] = None,
    ) -> HeartbeatPayload:
        """Generate and transmit a heartbeat payload to the coordinator registry."""
        telemetry = metrics or self.collect_telemetry()
        payload = HeartbeatPayload(
            worker_id=self.worker_id,
            timestamp=datetime.now(timezone.utc),
            status=status,
            metrics=telemetry,
            active_task_ids=list(self.active_tasks),
        )
        self.registry.record_heartbeat(self.worker_id, payload)
        return payload

