"""Tests for edge worker registration and heartbeat monitoring REST API endpoints."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.api.app import app
from backend.edge import edge_worker_registry

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_edge_registry():
    """Ensure clean registry before and after every API test."""
    edge_worker_registry.clear()
    yield
    edge_worker_registry.clear()


class TestEdgeWorkerEndpoints:
    """Tests for edge worker registration, lifecycle, and query endpoints."""

    def test_register_worker_api_success(self):
        payload = {
            "name": "Pune North Tehsil Node",
            "district": "Pune",
            "state": "Maharashtra",
            "ip_address": "192.168.1.55",
            "port": 8080,
            "capabilities": ["CADASTRAL_INGESTION", "TOPOLOGY_CLEANING"],
            "tags": {"environment": "production", "zone": "North"},
            "heartbeat_timeout_sec": 30.0,
        }
        resp = client.post("/api/v1/edge/workers/register", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Pune North Tehsil Node"
        assert data["district"] == "Pune"
        assert data["state"] == "Maharashtra"
        assert data["status"] == "ONLINE"
        assert "CADASTRAL_INGESTION" in data["capabilities"]
        assert "TOPOLOGY_CLEANING" in data["capabilities"]
        assert data["worker_id"].startswith("edge-pune-")
        assert data["auth_token"] is not None

    def test_register_worker_api_invalid_payload(self):
        # Invalid port > 65535
        payload = {
            "name": "Invalid Node",
            "district": "Pune",
            "state": "Maharashtra",
            "ip_address": "192.168.1.1",
            "port": 99999,
        }
        resp = client.post("/api/v1/edge/workers/register", json=payload)
        assert resp.status_code in (400, 422)

        # Invalid IP address
        payload_bad_ip = {
            "name": "Invalid IP Node",
            "district": "Pune",
            "state": "Maharashtra",
            "ip_address": "invalid-ip-string",
            "port": 8080,
        }
        resp2 = client.post("/api/v1/edge/workers/register", json=payload_bad_ip)
        assert resp2.status_code == 400

    def test_list_and_filter_workers_api(self):
        # Register 2 workers
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Pune Node",
                "district": "Pune",
                "state": "Maharashtra",
                "ip_address": "192.168.1.1",
                "port": 8001,
                "capabilities": ["CADASTRAL_INGESTION"],
                "worker_id": "w-pune-1",
            },
        )
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Satara Node",
                "district": "Satara",
                "state": "Maharashtra",
                "ip_address": "192.168.2.1",
                "port": 8002,
                "capabilities": ["TOPOLOGY_CLEANING"],
                "worker_id": "w-satara-1",
            },
        )

        # List all
        resp_all = client.get("/api/v1/edge/workers")
        assert resp_all.status_code == 200
        assert len(resp_all.json()) == 2

        # Filter by district
        resp_pune = client.get("/api/v1/edge/workers?district=Pune")
        assert resp_pune.status_code == 200
        assert len(resp_pune.json()) == 1
        assert resp_pune.json()[0]["worker_id"] == "w-pune-1"

        # Filter by capability
        resp_topo = client.get("/api/v1/edge/workers?capability=TOPOLOGY_CLEANING")
        assert resp_topo.status_code == 200
        assert len(resp_topo.json()) == 1
        assert resp_topo.json()[0]["worker_id"] == "w-satara-1"

    def test_get_worker_detail_and_not_found(self):
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Detail Node",
                "district": "Thane",
                "state": "Maharashtra",
                "ip_address": "192.168.3.1",
                "port": 8080,
                "worker_id": "w-thane-detail",
            },
        )

        resp = client.get("/api/v1/edge/workers/w-thane-detail")
        assert resp.status_code == 200
        assert resp.json()["worker_id"] == "w-thane-detail"

        # Nonexistent
        resp_404 = client.get("/api/v1/edge/workers/ghost-node")
        assert resp_404.status_code == 404

    def test_record_heartbeat_api(self):
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Heartbeat Target",
                "district": "Pune",
                "state": "Maharashtra",
                "ip_address": "192.168.1.100",
                "port": 8080,
                "worker_id": "w-hb-target",
            },
        )

        hb_payload = {
            "status": "BUSY",
            "metrics": {
                "cpu_usage_pct": 25.5,
                "memory_usage_pct": 55.0,
                "memory_available_mb": 3000.0,
                "disk_free_gb": 120.0,
                "active_tasks": 3,
                "completed_tasks": 12,
                "uptime_seconds": 1800.0,
            },
            "active_task_ids": ["t-1", "t-2", "t-3"],
        }
        resp = client.post("/api/v1/edge/workers/w-hb-target/heartbeat", json=hb_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["worker_id"] == "w-hb-target"
        assert data["status"] == "BUSY"
        assert data["consecutive_missed_heartbeats"] == 0

        # Verify get detail reflects updated status and metrics
        detail_resp = client.get("/api/v1/edge/workers/w-hb-target")
        detail = detail_resp.json()
        assert detail["status"] == "BUSY"
        assert detail["metrics"]["cpu_usage_pct"] == 25.5
        assert detail["metrics"]["active_tasks"] == 3

    def test_record_heartbeat_unregistered_node(self):
        resp = client.post("/api/v1/edge/workers/unknown-node/heartbeat", json={"status": "ONLINE"})
        assert resp.status_code == 404

    def test_deregister_worker_api(self):
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Decommission Target",
                "district": "Pune",
                "state": "Maharashtra",
                "ip_address": "192.168.1.200",
                "port": 8080,
                "worker_id": "w-decom-target",
            },
        )

        resp = client.delete("/api/v1/edge/workers/w-decom-target")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["worker_id"] == "w-decom-target"

        # Subsequent GET should return 404
        assert client.get("/api/v1/edge/workers/w-decom-target").status_code == 404

        # Deregistering again returns 404
        assert client.delete("/api/v1/edge/workers/w-decom-target").status_code == 404

    def test_find_eligible_workers_api(self):
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Eligible Ingestion Node",
                "district": "Pune",
                "state": "Maharashtra",
                "ip_address": "192.168.1.10",
                "port": 8001,
                "capabilities": ["CADASTRAL_INGESTION"],
                "worker_id": "w-el-1",
            },
        )
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Eligible Transform Node",
                "district": "Pune",
                "state": "Maharashtra",
                "ip_address": "192.168.1.11",
                "port": 8002,
                "capabilities": ["GEODETIC_TRANSFORMATION"],
                "worker_id": "w-el-2",
            },
        )

        resp = client.get("/api/v1/edge/workers/eligible?capability=GEODETIC_TRANSFORMATION")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["worker_id"] == "w-el-2"

    def test_cluster_health_summary_api(self):
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Health Node 1",
                "district": "Pune",
                "state": "Maharashtra",
                "ip_address": "192.168.1.1",
                "port": 8001,
                "worker_id": "w-h-1",
            },
        )
        client.post(
            "/api/v1/edge/workers/register",
            json={
                "name": "Health Node 2",
                "district": "Pune",
                "state": "Maharashtra",
                "ip_address": "192.168.1.2",
                "port": 8002,
                "worker_id": "w-h-2",
            },
        )

        # Send heartbeat with telemetry for Node 1
        client.post(
            "/api/v1/edge/workers/w-h-1/heartbeat",
            json={
                "status": "ONLINE",
                "metrics": {
                    "cpu_usage_pct": 20.0,
                    "memory_usage_pct": 40.0,
                    "memory_available_mb": 2048.0,
                    "disk_free_gb": 100.0,
                    "active_tasks": 2,
                    "completed_tasks": 8,
                },
            },
        )

        resp = client.get("/api/v1/edge/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_workers"] == 2
        assert data["online_count"] == 2
        assert data["active_workers_count"] == 2
        assert data["total_active_tasks"] == 2
        assert data["total_completed_tasks"] == 8
        assert data["average_cpu_pct"] == 20.0

