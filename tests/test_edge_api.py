"""Tests for edge worker registration and heartbeat monitoring REST API endpoints."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.api.app import app
from backend.edge import edge_worker_registry, sync_coordinator

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_edge_registry():
    """Ensure clean registry and sync coordinator before and after every API test."""
    edge_worker_registry.clear()
    sync_coordinator.clear()
    yield
    edge_worker_registry.clear()
    sync_coordinator.clear()


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


class TestOfflineSyncEndpoints:
    """Tests for offline sync batch submission, conflict resolution, and dispute escalation."""

    def test_sync_batch_clean_commit(self):
        from backend.edge.sync import compute_payload_checksum
        from datetime import datetime, timezone

        p1 = {"parcel_id": "P-101", "area_sqm": 2400.0, "owner": "Farmer A"}
        p2 = {"parcel_id": "P-102", "area_sqm": 1850.0, "owner": "Farmer B"}
        ts = datetime.now(timezone.utc).isoformat()

        batch_payload = {
            "worker_id": "w-pune-sync",
            "items": [
                {
                    "operation_id": "op-1",
                    "worker_id": "w-pune-sync",
                    "entity_type": "PARCEL",
                    "entity_id": "P-101",
                    "operation_type": "INSERT_PARCEL",
                    "sequence_number": 1,
                    "client_timestamp": ts,
                    "base_version": 0,
                    "payload": p1,
                    "checksum": compute_payload_checksum(p1),
                    "idempotency_key": "w-pune-sync:P-101:1",
                },
                {
                    "operation_id": "op-2",
                    "worker_id": "w-pune-sync",
                    "entity_type": "PARCEL",
                    "entity_id": "P-102",
                    "operation_type": "INSERT_PARCEL",
                    "sequence_number": 2,
                    "client_timestamp": ts,
                    "base_version": 0,
                    "payload": p2,
                    "checksum": compute_payload_checksum(p2),
                    "idempotency_key": "w-pune-sync:P-102:2",
                },
            ],
        }

        resp = client.post("/api/v1/edge/sync/batch", json=batch_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_items"] == 2
        assert data["committed_count"] == 2
        assert data["conflict_count"] == 0
        assert data["failed_count"] == 0
        assert "op-1" in data["acknowledged_operation_ids"]
        assert "op-2" in data["acknowledged_operation_ids"]

        # Verify get entity version
        v_resp = client.get("/api/v1/edge/sync/entity/P-101")
        assert v_resp.status_code == 200
        v_data = v_resp.json()
        assert v_data["entity_id"] == "P-101"
        assert v_data["version"] == 1
        assert v_data["payload"]["area_sqm"] == 2400.0

    def test_sync_batch_conflict_resolution_edge_wins(self):
        from backend.edge.sync import compute_payload_checksum
        from datetime import datetime, timezone

        # Seed server entity at version 5
        sync_coordinator.set_server_entity(
            entity_id="P-200",
            version=5,
            payload={"parcel_id": "P-200", "area_sqm": 1000.0},
        )

        p_edge = {"parcel_id": "P-200", "area_sqm": 1250.0, "source": "field_gnss"}
        ts = datetime.now(timezone.utc).isoformat()

        batch_payload = {
            "worker_id": "w-edge-gps",
            "policy": "EDGE_WINS",
            "items": [
                {
                    "operation_id": "op-edge-win",
                    "worker_id": "w-edge-gps",
                    "entity_type": "PARCEL",
                    "entity_id": "P-200",
                    "operation_type": "UPDATE_PARCEL",
                    "sequence_number": 1,
                    "client_timestamp": ts,
                    "base_version": 2,  # Drifted from server version 5
                    "payload": p_edge,
                    "checksum": compute_payload_checksum(p_edge),
                    "idempotency_key": "w-edge-gps:P-200:1",
                }
            ],
        }

        resp = client.post("/api/v1/edge/sync/batch", json=batch_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["conflict_count"] == 1
        assert len(data["conflicts"]) == 1
        conf = data["conflicts"][0]
        assert conf["policy_applied"] == "EDGE_WINS"
        assert conf["resolved_version"] == 6
        assert conf["is_disputed"] is False

        # Verify coordinator state was updated
        server_rec = sync_coordinator.get_server_entity("P-200")
        assert server_rec.version == 6
        assert server_rec.payload["area_sqm"] == 1250.0

    def test_sync_batch_conflict_resolution_manual_dispute(self):
        from backend.edge.sync import compute_payload_checksum
        from datetime import datetime, timezone

        # Seed server entity
        sync_coordinator.set_server_entity(
            entity_id="DISPUTED-PARCEL-1",
            version=4,
            payload={"parcel_id": "DISPUTED-PARCEL-1", "area_sqm": 500.0},
        )

        client_p = {"parcel_id": "DISPUTED-PARCEL-1", "area_sqm": 900.0}
        ts = datetime.now(timezone.utc).isoformat()

        batch_payload = {
            "worker_id": "w-field-surveyor",
            "policy": "MANUAL_DISPUTE",
            "items": [
                {
                    "operation_id": "op-disp-1",
                    "worker_id": "w-field-surveyor",
                    "entity_type": "PARCEL",
                    "entity_id": "DISPUTED-PARCEL-1",
                    "operation_type": "UPDATE_PARCEL",
                    "sequence_number": 1,
                    "client_timestamp": ts,
                    "base_version": 2,
                    "payload": client_p,
                    "checksum": compute_payload_checksum(client_p),
                    "idempotency_key": "w-field-surveyor:DISPUTED-PARCEL-1:1",
                }
            ],
        }

        resp = client.post("/api/v1/edge/sync/batch", json=batch_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["conflict_count"] == 1
        conf = data["conflicts"][0]
        assert conf["is_disputed"] is True
        assert conf["policy_applied"] == "MANUAL_DISPUTE"

        # Check entity version lookup shows disputed flag
        v_resp = client.get("/api/v1/edge/sync/entity/DISPUTED-PARCEL-1")
        assert v_resp.status_code == 200
        assert v_resp.json()["is_disputed"] is True
        assert v_resp.json()["payload"]["dispute_locked"] is True

        # Now resolve the dispute
        resolve_payload = {
            "resolved_payload": {"parcel_id": "DISPUTED-PARCEL-1", "area_sqm": 700.0, "notes": "Tehsildar joint measurement"},
            "resolution_method": "TEHSILDAR_SIGN_OFF",
            "resolved_by": "OFFICER-SHINDE-01",
        }
        res_resp = client.post("/api/v1/edge/sync/disputes/DISPUTED-PARCEL-1/resolve", json=resolve_payload)
        assert res_resp.status_code == 200
        res_data = res_resp.json()
        assert res_data["success"] is True
        assert res_data["is_disputed"] is False
        assert res_data["new_version"] == 5

        # Check entity is no longer disputed
        v_after = client.get("/api/v1/edge/sync/entity/DISPUTED-PARCEL-1").json()
        assert v_after["is_disputed"] is False
        assert v_after["payload"]["area_sqm"] == 700.0

    def test_sync_batch_checksum_failure(self):
        from datetime import datetime, timezone

        p = {"parcel_id": "P-TAMPER", "val": 1}
        ts = datetime.now(timezone.utc).isoformat()

        batch_payload = {
            "worker_id": "w-tamper",
            "items": [
                {
                    "operation_id": "op-tamper",
                    "worker_id": "w-tamper",
                    "entity_type": "PARCEL",
                    "entity_id": "P-TAMPER",
                    "operation_type": "INSERT_PARCEL",
                    "sequence_number": 1,
                    "client_timestamp": ts,
                    "base_version": 0,
                    "payload": p,
                    "checksum": "corrupted_checksum_hex",
                    "idempotency_key": "w-tamper:P-TAMPER:1",
                }
            ],
        }

        resp = client.post("/api/v1/edge/sync/batch", json=batch_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["failed_count"] == 1
        assert data["committed_count"] == 0
        assert len(data["acknowledged_operation_ids"]) == 0

    def test_get_entity_not_found(self):
        resp = client.get("/api/v1/edge/sync/entity/NONEXISTENT-ENTITY")
        assert resp.status_code == 404

    def test_resolve_dispute_not_found(self):
        resp = client.post(
            "/api/v1/edge/sync/disputes/NONEXISTENT/resolve",
            json={"resolved_payload": {"k": "v"}, "resolved_by": "OFFICER"},
        )
        assert resp.status_code == 404


class TestDeltaTransmissionEndpoints:
    """Tests for chunked, compressed delta transmission API endpoints."""

    def test_delta_transmission_api_roundtrip(self):
        import base64
        from backend.edge.delta import (
            DeltaCompressionType,
            DeltaSyncEngine,
            NetworkTier,
        )

        base_state = {
            "PARCEL-1": {"owner": "Ram", "area": 1000.0},
            "PARCEL-2": {"owner": "Shyam", "area": 2000.0},
        }
        current_state = {
            "PARCEL-1": {"owner": "Ram Kumar", "area": 1050.0},  # modified
            "PARCEL-3": {"owner": "Gita", "area": 1500.0},        # added (PARCEL-2 deleted)
        }

        # Seed initial state in coordinator
        for pid, pdata in base_state.items():
            sync_coordinator.set_server_entity(pid, version=1, payload=pdata)

        engine = DeltaSyncEngine(
            worker_id="w-pune-delta",
            network_tier=NetworkTier.TIER_3G,
            compression=DeltaCompressionType.GZIP,
            chunk_size_bytes=64,
        )

        package = engine.prepare_sync_package(base_state, current_state)
        assert package.total_chunks > 0

        # Encode chunks to base64 schemas
        chunk_schemas = []
        for ch in package.chunks:
            chunk_schemas.append(
                {
                    "delta_id": ch.delta_id,
                    "chunk_index": ch.chunk_index,
                    "total_chunks": ch.total_chunks,
                    "chunk_data_b64": base64.b64encode(ch.chunk_data).decode("ascii"),
                    "checksum": ch.checksum,
                }
            )

        submit_payload = {
            "worker_id": "w-pune-delta",
            "delta_id": package.delta_id,
            "compression": "GZIP",
            "network_tier": "3G",
            "chunks": chunk_schemas,
        }

        resp = client.post("/api/v1/edge/delta/transmit", json=submit_payload)
        assert resp.status_code == 200
        res_data = resp.json()
        assert res_data["success"] is True
        assert res_data["delta_id"] == package.delta_id
        assert res_data["total_entities_affected"] == 3
        assert res_data["added_count"] == 1
        assert res_data["modified_count"] == 1
        assert res_data["deleted_count"] == 1

        # Verify server state reflects delta
        p1 = sync_coordinator.get_server_entity("PARCEL-1")
        assert p1 is not None
        assert p1.payload["owner"] == "Ram Kumar"
        assert p1.version == 2

        p3 = sync_coordinator.get_server_entity("PARCEL-3")
        assert p3 is not None
        assert p3.payload["owner"] == "Gita"

        assert sync_coordinator.get_server_entity("PARCEL-2") is None

    def test_delta_transmission_corrupted_chunk_rejected(self):
        import base64

        chunk_schemas = [
            {
                "delta_id": "delta-fail",
                "chunk_index": 0,
                "total_chunks": 1,
                "chunk_data_b64": base64.b64encode(b"bad-data").decode("ascii"),
                "checksum": "wrong_checksum_hex",
            }
        ]
        submit_payload = {
            "worker_id": "w-fail",
            "delta_id": "delta-fail",
            "compression": "GZIP",
            "chunks": chunk_schemas,
        }
        resp = client.post("/api/v1/edge/delta/transmit", json=submit_payload)
        assert resp.status_code == 400
        assert "Checksum verification failed" in resp.json()["detail"]



