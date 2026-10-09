"""Heartbeat monitoring and liveness tracking service for edge workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.edge.models import EdgeWorker, WorkerStatus
from backend.edge.registry import EdgeWorkerRegistry, edge_worker_registry

logger = logging.getLogger(__name__)


@dataclass
class WorkerStateTransition:
    """Record of an automated worker status change caused by heartbeat events."""

    worker_id: str
    previous_status: WorkerStatus
    new_status: WorkerStatus
    reason: str
    timestamp: datetime
    elapsed_seconds: float


class HeartbeatMonitor:
    """Liveness probe and health evaluator for registered edge compute workers."""

    def __init__(self, registry: Optional[EdgeWorkerRegistry] = None) -> None:
        self.registry = registry or edge_worker_registry

    def check_liveness(
        self,
        now: Optional[datetime] = None,
    ) -> List[WorkerStateTransition]:
        """Evaluate heartbeat freshness across all workers and apply state transitions."""
        current_time = now or datetime.now(timezone.utc)
        transitions: List[WorkerStateTransition] = []

        all_workers = self.registry.list_workers()

        for worker in all_workers:
            if worker.status in (WorkerStatus.DEREGISTERED, WorkerStatus.DRAINING):
                continue

            elapsed = (current_time - worker.last_heartbeat_at).total_seconds()
            prev_status = worker.status

            if elapsed > worker.heartbeat_timeout_sec:
                worker.consecutive_missed_heartbeats += 1

                # If missed multiple heartbeats or elapsed >= 2x timeout, transition to OFFLINE
                if (
                    worker.consecutive_missed_heartbeats >= 3
                    or elapsed >= worker.heartbeat_timeout_sec * 2.0
                ):
                    target_status = WorkerStatus.OFFLINE
                else:
                    target_status = WorkerStatus.DEGRADED

                if prev_status != target_status:
                    worker.status = target_status
                    trans = WorkerStateTransition(
                        worker_id=worker.worker_id,
                        previous_status=prev_status,
                        new_status=target_status,
                        reason=f"Missed heartbeat: {elapsed:.1f}s elapsed (timeout: {worker.heartbeat_timeout_sec}s)",
                        timestamp=current_time,
                        elapsed_seconds=elapsed,
                    )
                    transitions.append(trans)
                    logger.warning(
                        "Worker %s transitioned from %s to %s (%s)",
                        worker.worker_id,
                        prev_status.value,
                        target_status.value,
                        trans.reason,
                    )

        return transitions

    def reap_stale_workers(
        self,
        max_offline_sec: float = 3600.0,
        now: Optional[datetime] = None,
    ) -> List[str]:
        """Deregister workers that have remained OFFLINE longer than max_offline_sec."""
        current_time = now or datetime.now(timezone.utc)
        reaped_ids: List[str] = []

        offline_workers = self.registry.list_workers(status=WorkerStatus.OFFLINE)
        for w in offline_workers:
            elapsed = (current_time - w.last_heartbeat_at).total_seconds()
            if elapsed >= max_offline_sec:
                self.registry.deregister_worker(w.worker_id, reason="stale_offline_reaped")
                reaped_ids.append(w.worker_id)
                logger.info("Reaped stale worker %s (offline for %.1fs)", w.worker_id, elapsed)

        return reaped_ids

    def get_cluster_health_summary(self) -> Dict[str, Any]:
        """Aggregate real-time cluster health and load metrics across all edge workers."""
        workers = self.registry.list_workers()
        total = len(workers)

        online_workers = [w for w in workers if w.status == WorkerStatus.ONLINE]
        idle_workers = [w for w in workers if w.status == WorkerStatus.IDLE]
        busy_workers = [w for w in workers if w.status == WorkerStatus.BUSY]
        degraded_workers = [w for w in workers if w.status == WorkerStatus.DEGRADED]
        offline_workers = [w for w in workers if w.status == WorkerStatus.OFFLINE]

        live_workers = [w for w in workers if w.is_alive and w.metrics]

        avg_cpu = (
            sum(w.metrics.cpu_usage_pct for w in live_workers) / len(live_workers)  # type: ignore
            if live_workers
            else 0.0
        )
        avg_mem = (
            sum(w.metrics.memory_usage_pct for w in live_workers) / len(live_workers)  # type: ignore
            if live_workers
            else 0.0
        )
        total_active_tasks = sum(
            w.metrics.active_tasks for w in live_workers if w.metrics  # type: ignore
        )
        total_completed_tasks = sum(
            w.metrics.completed_tasks for w in live_workers if w.metrics  # type: ignore
        )

        return {
            "total_workers": total,
            "online_count": len(online_workers),
            "idle_count": len(idle_workers),
            "busy_count": len(busy_workers),
            "degraded_count": len(degraded_workers),
            "offline_count": len(offline_workers),
            "active_workers_count": len(online_workers) + len(idle_workers) + len(busy_workers),
            "average_cpu_pct": round(avg_cpu, 2),
            "average_memory_pct": round(avg_mem, 2),
            "total_active_tasks": total_active_tasks,
            "total_completed_tasks": total_completed_tasks,
        }


# Global default monitor instance
heartbeat_monitor = HeartbeatMonitor()
