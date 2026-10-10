"""Comprehensive test suite for edge worker registration and heartbeat monitoring.

Validates:
1. Decentralized edge node registration, validation, and lifecycle states.
2. Hardware telemetry logging (CPU, RAM, disk, active tasks).
3. Periodic heartbeat monitoring, liveness detection, and automated timeout transitions.
4. Worker degradation and offline failover transitions upon missed heartbeats.
5. Automated recovery of degraded/offline nodes upon heartbeat receipt.
6. Workload-aware task routing based on capability, administrative district, and CPU load.
7. Cluster health summary metrics aggregation and stale worker reaping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List
import pytest

from backend.edge.client import EdgeHeartbeatClient
from backend.edge.models import (
    EdgeWorker,
    HardwareMetrics,
    HeartbeatPayload,
    WorkerCapability,
    WorkerNotFoundError,
    WorkerRegistrationError,
    WorkerStatus,
)
from backend.edge.monitor import HeartbeatMonitor, WorkerStateTransition
from backend.edge.registry import EdgeWorkerRegistry


@pytest.fixture
def registry() -> EdgeWorkerRegistry:
    """Isolated EdgeWorkerRegistry instance for each test."""
    return EdgeWorkerRegistry()


@pytest.fixture
def monitor(registry: EdgeWorkerRegistry) -> HeartbeatMonitor:
    """HeartbeatMonitor attached to the isolated test registry."""
    return HeartbeatMonitor(registry=registry)


# =====================================================================
# 1. Edge Worker Registration Tests
# =====================================================================

class TestEdgeWorkerRegistration:
    """Tests for edge worker registration, attribute validation, and catalog queries."""

    def test_register_worker_success(self, registry: EdgeWorkerRegistry):
        worker = registry.register_worker(
            name="Pune Tehsil Cadastral Node 1",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.50",
            port=8080,
            capabilities=[
                WorkerCapability.CADASTRAL_INGESTION,
                WorkerCapability.TOPOLOGY_CLEANING,
            ],
            tags={"office_type": "Tehsil", "rack": "A1"},
            heartbeat_timeout_sec=25.0,
        )

        assert isinstance(worker, EdgeWorker)
        assert worker.worker_id.startswith("edge-pune-")
        assert worker.name == "Pune Tehsil Cadastral Node 1"
        assert worker.district == "Pune"
        assert worker.state == "Maharashtra"
        assert worker.ip_address == "192.168.1.50"
        assert worker.port == 8080
        assert worker.status == WorkerStatus.ONLINE
        assert worker.is_alive is True
        assert WorkerCapability.CADASTRAL_INGESTION in worker.capabilities
        assert WorkerCapability.TOPOLOGY_CLEANING in worker.capabilities
        assert worker.tags["office_type"] == "Tehsil"
        assert worker.heartbeat_timeout_sec == 25.0
        assert worker.auth_token is not None
        assert registry.count() == 1

    def test_register_worker_custom_id(self, registry: EdgeWorkerRegistry):
        custom_id = "edge-node-nagpur-custom-007"
        worker = registry.register_worker(
            name="Nagpur Field Unit",
            district="Nagpur",
            state="Maharashtra",
            ip_address="10.0.0.12",
            port=9000,
            worker_id=custom_id,
        )

        assert worker.worker_id == custom_id
        assert registry.get_worker(custom_id) is worker

    @pytest.mark.parametrize(
        "name, district, state, ip, port, err_match",
        [
            ("", "Pune", "MH", "192.168.1.1", 8080, "Worker name cannot be empty"),
            ("Node1", "", "MH", "192.168.1.1", 8080, "district cannot be empty"),
            ("Node1", "Pune", "", "192.168.1.1", 8080, "State cannot be empty"),
            ("Node1", "Pune", "MH", "999.999.999.999", 8080, "Invalid IP address"),
            ("Node1", "Pune", "MH", "not-an-ip", 8080, "Invalid IP address"),
            ("Node1", "Pune", "MH", "192.168.1.1", 0, "Port must be between 1 and 65535"),
            ("Node1", "Pune", "MH", "192.168.1.1", 70000, "Port must be between 1 and 65535"),
        ],
    )
    def test_register_worker_validation_errors(
        self, registry: EdgeWorkerRegistry, name, district, state, ip, port, err_match
    ):
        with pytest.raises(WorkerRegistrationError, match=err_match):
            registry.register_worker(
                name=name,
                district=district,
                state=state,
                ip_address=ip,
                port=port,
            )

    def test_register_worker_update_existing(self, registry: EdgeWorkerRegistry):
        w1 = registry.register_worker(
            name="Initial Name",
            district="Thane",
            state="Maharashtra",
            ip_address="192.168.1.10",
            port=8080,
            worker_id="edge-thane-01",
        )
        w1.consecutive_missed_heartbeats = 4
        w1.status = WorkerStatus.OFFLINE

        # Re-register with updated config
        w2 = registry.register_worker(
            name="Updated Name",
            district="Thane",
            state="Maharashtra",
            ip_address="192.168.1.15",
            port=8081,
            capabilities=[WorkerCapability.GEODETIC_TRANSFORMATION],
            worker_id="edge-thane-01",
        )

        assert w1 is w2
        assert w2.name == "Updated Name"
        assert w2.port == 8081
        assert w2.status == WorkerStatus.ONLINE
        assert w2.consecutive_missed_heartbeats == 0
        assert WorkerCapability.GEODETIC_TRANSFORMATION in w2.capabilities
        assert registry.count() == 1

    def test_get_worker_and_raise_behavior(self, registry: EdgeWorkerRegistry):
        assert registry.get_worker("nonexistent-worker") is None

        with pytest.raises(WorkerNotFoundError, match="is not registered"):
            registry.get_worker_or_raise("nonexistent-worker")

    def test_list_workers_filtering(self, registry: EdgeWorkerRegistry):
        registry.register_worker(
            name="Pune 1",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.1",
            port=8001,
            capabilities=[WorkerCapability.CADASTRAL_INGESTION],
            worker_id="w-pune-1",
        )
        registry.register_worker(
            name="Pune 2",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.2",
            port=8002,
            capabilities=[WorkerCapability.TOPOLOGY_CLEANING],
            worker_id="w-pune-2",
        )
        w_satara = registry.register_worker(
            name="Satara 1",
            district="Satara",
            state="Maharashtra",
            ip_address="192.168.2.1",
            port=8003,
            capabilities=[WorkerCapability.CADASTRAL_INGESTION],
            worker_id="w-satara-1",
        )
        registry.set_worker_status(w_satara.worker_id, WorkerStatus.BUSY)

        # Filter by district
        pune_workers = registry.list_workers(district="Pune")
        assert len(pune_workers) == 2

        # Filter by status
        busy_workers = registry.list_workers(status=WorkerStatus.BUSY)
        assert len(busy_workers) == 1
        assert busy_workers[0].worker_id == "w-satara-1"

        # Filter by capability
        topo_workers = registry.list_workers(capability=WorkerCapability.TOPOLOGY_CLEANING)
        assert len(topo_workers) == 1
        assert topo_workers[0].worker_id == "w-pune-2"

    def test_deregister_worker(self, registry: EdgeWorkerRegistry):
        worker = registry.register_worker(
            name="Temp Node",
            district="Nashik",
            state="Maharashtra",
            ip_address="192.168.3.1",
            port=8080,
            worker_id="w-nashik-temp",
        )
        assert registry.count() == 1

        success = registry.deregister_worker("w-nashik-temp", reason="maintenance")
        assert success is True
        assert registry.count() == 0
        assert registry.get_worker("w-nashik-temp") is None
        assert worker.status == WorkerStatus.DEREGISTERED

        # Deregister nonexistent returns False
        assert registry.deregister_worker("unknown") is False

    def test_worker_to_dict_serialization(self, registry: EdgeWorkerRegistry):
        worker = registry.register_worker(
            name="Serialization Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.99",
            port=8080,
            capabilities=[WorkerCapability.CADASTRAL_INGESTION],
            worker_id="w-pune-ser",
        )
        worker.metrics = HardwareMetrics(
            cpu_usage_pct=22.5,
            memory_usage_pct=45.0,
            memory_available_mb=2048.0,
            disk_free_gb=80.5,
            active_tasks=2,
            completed_tasks=10,
            uptime_seconds=3600.0,
        )

        d = worker.to_dict()
        assert d["worker_id"] == "w-pune-ser"
        assert d["district"] == "Pune"
        assert d["status"] == "ONLINE"
        assert "CADASTRAL_INGESTION" in d["capabilities"]
        assert d["metrics"]["cpu_usage_pct"] == 22.5
        assert d["metrics"]["active_tasks"] == 2


# =====================================================================
# 2. Heartbeat Monitoring & Liveness Tests
# =====================================================================

class TestHeartbeatMonitoring:
    """Tests for heartbeat processing, liveness checks, and state degradation."""

    def test_record_heartbeat_success(self, registry: EdgeWorkerRegistry):
        worker = registry.register_worker(
            name="Heartbeat Node",
            district="Kolhapur",
            state="Maharashtra",
            ip_address="192.168.4.10",
            port=8080,
            worker_id="w-kolhapur-1",
        )
        initial_hb_time = worker.last_heartbeat_at

        # Simulate small delay
        hb_payload = HeartbeatPayload(
            worker_id="w-kolhapur-1",
            status=WorkerStatus.IDLE,
            metrics=HardwareMetrics(
                cpu_usage_pct=10.0,
                memory_usage_pct=25.0,
                memory_available_mb=3000.0,
                disk_free_gb=150.0,
            ),
            active_task_ids=["task-101"],
        )

        updated_worker = registry.record_heartbeat("w-kolhapur-1", hb_payload)

        assert updated_worker.status == WorkerStatus.IDLE
        assert updated_worker.last_heartbeat_at >= initial_hb_time
        assert updated_worker.metrics is not None
        assert updated_worker.metrics.cpu_usage_pct == 10.0
        assert updated_worker.consecutive_missed_heartbeats == 0

    def test_record_heartbeat_unregistered_raises(self, registry: EdgeWorkerRegistry):
        with pytest.raises(WorkerNotFoundError):
            registry.record_heartbeat("ghost-node")

    def test_worker_liveness_healthy(
        self, registry: EdgeWorkerRegistry, monitor: HeartbeatMonitor
    ):
        worker = registry.register_worker(
            name="Healthy Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.20",
            port=8080,
            heartbeat_timeout_sec=30.0,
            worker_id="w-healthy",
        )
        now = datetime.now(timezone.utc)
        worker.last_heartbeat_at = now - timedelta(seconds=10.0)  # 10s < 30s timeout

        transitions = monitor.check_liveness(now=now)

        assert len(transitions) == 0
        assert worker.status == WorkerStatus.ONLINE
        assert worker.consecutive_missed_heartbeats == 0

    def test_worker_liveness_degraded_transition(
        self, registry: EdgeWorkerRegistry, monitor: HeartbeatMonitor
    ):
        worker = registry.register_worker(
            name="Degraded Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.21",
            port=8080,
            heartbeat_timeout_sec=30.0,
            worker_id="w-degraded",
        )
        now = datetime.now(timezone.utc)
        # Missed 1 heartbeat: elapsed 35s (> 30s timeout but < 2x timeout)
        worker.last_heartbeat_at = now - timedelta(seconds=35.0)

        transitions = monitor.check_liveness(now=now)

        assert len(transitions) == 1
        t = transitions[0]
        assert t.worker_id == "w-degraded"
        assert t.previous_status == WorkerStatus.ONLINE
        assert t.new_status == WorkerStatus.DEGRADED
        assert worker.status == WorkerStatus.DEGRADED
        assert worker.consecutive_missed_heartbeats == 1

    def test_worker_liveness_offline_transition(
        self, registry: EdgeWorkerRegistry, monitor: HeartbeatMonitor
    ):
        worker = registry.register_worker(
            name="Offline Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.22",
            port=8080,
            heartbeat_timeout_sec=30.0,
            worker_id="w-offline",
        )
        now = datetime.now(timezone.utc)
        # Elapsed 70s (> 2x 30s timeout)
        worker.last_heartbeat_at = now - timedelta(seconds=70.0)

        transitions = monitor.check_liveness(now=now)

        assert len(transitions) == 1
        t = transitions[0]
        assert t.worker_id == "w-offline"
        assert t.new_status == WorkerStatus.OFFLINE
        assert worker.status == WorkerStatus.OFFLINE

    def test_worker_recovery_on_heartbeat(
        self, registry: EdgeWorkerRegistry, monitor: HeartbeatMonitor
    ):
        worker = registry.register_worker(
            name="Recovering Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.23",
            port=8080,
            heartbeat_timeout_sec=30.0,
            worker_id="w-recovering",
        )
        worker.status = WorkerStatus.OFFLINE
        worker.consecutive_missed_heartbeats = 5

        # Worker sends a fresh heartbeat packet
        hb = HeartbeatPayload(
            worker_id="w-recovering",
            status=WorkerStatus.ONLINE,
            metrics=HardwareMetrics(cpu_usage_pct=12.0, memory_usage_pct=30.0, memory_available_mb=2048.0, disk_free_gb=50.0),
        )
        registry.record_heartbeat("w-recovering", hb)

        assert worker.status == WorkerStatus.ONLINE
        assert worker.consecutive_missed_heartbeats == 0
        assert worker.is_alive is True

    def test_reap_stale_offline_workers(
        self, registry: EdgeWorkerRegistry, monitor: HeartbeatMonitor
    ):
        w_active = registry.register_worker(
            name="Active",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.1",
            port=8001,
            worker_id="w-active",
        )
        w_stale = registry.register_worker(
            name="Stale Dead Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.2",
            port=8002,
            worker_id="w-stale",
        )
        now = datetime.now(timezone.utc)
        w_stale.status = WorkerStatus.OFFLINE
        w_stale.last_heartbeat_at = now - timedelta(hours=3)

        reaped = monitor.reap_stale_workers(max_offline_sec=3600.0, now=now)

        assert len(reaped) == 1
        assert "w-stale" in reaped
        assert registry.get_worker("w-stale") is None
        assert registry.get_worker("w-active") is not None

    def test_cluster_health_summary(
        self, registry: EdgeWorkerRegistry, monitor: HeartbeatMonitor
    ):
        w1 = registry.register_worker(
            name="Node 1", district="Pune", state="MH", ip_address="192.168.1.1", port=8001, worker_id="w1"
        )
        w1.status = WorkerStatus.ONLINE
        w1.metrics = HardwareMetrics(cpu_usage_pct=20.0, memory_usage_pct=40.0, memory_available_mb=2048, disk_free_gb=100, active_tasks=2, completed_tasks=10)

        w2 = registry.register_worker(
            name="Node 2", district="Pune", state="MH", ip_address="192.168.1.2", port=8002, worker_id="w2"
        )
        w2.status = WorkerStatus.IDLE
        w2.metrics = HardwareMetrics(cpu_usage_pct=10.0, memory_usage_pct=30.0, memory_available_mb=4096, disk_free_gb=200, active_tasks=0, completed_tasks=5)

        w3 = registry.register_worker(
            name="Node 3", district="Pune", state="MH", ip_address="192.168.1.3", port=8003, worker_id="w3"
        )
        w3.status = WorkerStatus.OFFLINE

        summary = monitor.get_cluster_health_summary()

        assert summary["total_workers"] == 3
        assert summary["online_count"] == 1
        assert summary["idle_count"] == 1
        assert summary["offline_count"] == 1
        assert summary["active_workers_count"] == 2
        assert summary["average_cpu_pct"] == pytest.approx(15.0)  # (20 + 10) / 2
        assert summary["average_memory_pct"] == pytest.approx(35.0)  # (40 + 30) / 2
        assert summary["total_active_tasks"] == 2
        assert summary["total_completed_tasks"] == 15


# =====================================================================
# 3. Workload-Aware Task Eligibility Tests
# =====================================================================

class TestWorkloadAwareTaskEligibility:
    """Tests for selecting optimal edge workers based on capability and resource load."""

    def test_find_eligible_workers_by_capability(self, registry: EdgeWorkerRegistry):
        w_ingest = registry.register_worker(
            name="Ingestion Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.1",
            port=8001,
            capabilities=[WorkerCapability.CADASTRAL_INGESTION],
            worker_id="w-ingest",
        )
        w_transform = registry.register_worker(
            name="Transform Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.2",
            port=8002,
            capabilities=[WorkerCapability.GEODETIC_TRANSFORMATION],
            worker_id="w-trans",
        )

        eligible = registry.find_eligible_workers(
            required_capability=WorkerCapability.GEODETIC_TRANSFORMATION
        )

        assert len(eligible) == 1
        assert eligible[0].worker_id == "w-trans"

    def test_find_eligible_workers_load_balancing_priority(
        self, registry: EdgeWorkerRegistry
    ):
        # 3 workers with Cadastral Ingestion capability
        w_busy = registry.register_worker(
            name="Busy High CPU",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.1",
            port=8001,
            capabilities=[WorkerCapability.CADASTRAL_INGESTION],
            worker_id="w-busy",
        )
        w_busy.status = WorkerStatus.BUSY
        w_busy.metrics = HardwareMetrics(cpu_usage_pct=75.0, memory_usage_pct=80.0, memory_available_mb=1000, disk_free_gb=50, active_tasks=5)

        w_idle_low = registry.register_worker(
            name="Idle Low CPU",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.2",
            port=8002,
            capabilities=[WorkerCapability.CADASTRAL_INGESTION],
            worker_id="w-idle-low",
        )
        w_idle_low.status = WorkerStatus.IDLE
        w_idle_low.metrics = HardwareMetrics(cpu_usage_pct=8.0, memory_usage_pct=20.0, memory_available_mb=4000, disk_free_gb=100, active_tasks=0)

        w_idle_high = registry.register_worker(
            name="Idle Medium CPU",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.3",
            port=8003,
            capabilities=[WorkerCapability.CADASTRAL_INGESTION],
            worker_id="w-idle-high",
        )
        w_idle_high.status = WorkerStatus.IDLE
        w_idle_high.metrics = HardwareMetrics(cpu_usage_pct=35.0, memory_usage_pct=30.0, memory_available_mb=3000, disk_free_gb=100, active_tasks=0)

        eligible = registry.find_eligible_workers(
            required_capability=WorkerCapability.CADASTRAL_INGESTION
        )

        assert len(eligible) == 3
        # IDLE with lowest CPU should be ranked first!
        assert eligible[0].worker_id == "w-idle-low"
        assert eligible[1].worker_id == "w-idle-high"
        assert eligible[2].worker_id == "w-busy"

    def test_overloaded_workers_excluded(self, registry: EdgeWorkerRegistry):
        w_overloaded = registry.register_worker(
            name="Melting Node",
            district="Pune",
            state="Maharashtra",
            ip_address="192.168.1.1",
            port=8001,
            capabilities=[WorkerCapability.TOPOLOGY_CLEANING],
            worker_id="w-hot",
        )
        w_overloaded.status = WorkerStatus.BUSY
        w_overloaded.metrics = HardwareMetrics(cpu_usage_pct=92.5, memory_usage_pct=90.0, memory_available_mb=200, disk_free_gb=10, active_tasks=8)

        eligible = registry.find_eligible_workers(
            required_capability=WorkerCapability.TOPOLOGY_CLEANING,
            max_cpu_pct=85.0,
        )

        assert len(eligible) == 0


# =====================================================================
# 4. Edge Heartbeat Client Tests
# =====================================================================

class TestEdgeHeartbeatClient:
    """Tests for simulated edge worker client sending telemetry to coordinator."""

    def test_client_telemetry_collection_and_heartbeat(
        self, registry: EdgeWorkerRegistry
    ):
        worker = registry.register_worker(
            name="Field Laptop",
            district="Satara",
            state="Maharashtra",
            ip_address="127.0.0.1",
            port=9100,
            worker_id="w-field-satara",
        )

        client = EdgeHeartbeatClient(
            worker_id="w-field-satara",
            registry=registry,
        )
        client.active_tasks.append("task-sync-mouza-42")

        telemetry = client.collect_telemetry(
            cpu_usage_pct=18.5,
            memory_usage_pct=42.0,
            memory_available_mb=3500.0,
            disk_free_gb=110.0,
        )

        assert telemetry.cpu_usage_pct == 18.5
        assert telemetry.active_tasks == 1
        assert telemetry.uptime_seconds >= 0.0

        payload = client.send_heartbeat(
            status=WorkerStatus.BUSY,
            metrics=telemetry,
        )

        assert payload.worker_id == "w-field-satara"
        assert payload.status == WorkerStatus.BUSY
        assert "task-sync-mouza-42" in payload.active_task_ids

        # Check registry state updated
        refreshed = registry.get_worker("w-field-satara")
        assert refreshed is not None
        assert refreshed.status == WorkerStatus.BUSY
        assert refreshed.metrics is not None
        assert refreshed.metrics.cpu_usage_pct == 18.5

