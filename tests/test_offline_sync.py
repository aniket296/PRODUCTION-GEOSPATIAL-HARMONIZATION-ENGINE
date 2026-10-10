"""Comprehensive test suite for edge offline queue synchronization and conflict resolution.

Validates:
1. Local offline transaction enqueuing with cryptographic checksums and monotonic sequencing.
2. Idempotency key deduplication and FIFO transmission batching.
3. Checksum verification and payload tamper detection.
4. Version drift collision detection between edge workers and central coordinator.
5. Conflict resolution policies:
   - LAST_WRITE_WINS (LWW timestamp arbitration)
   - COORDINATOR_WINS (Central authority precedence)
   - EDGE_WINS (Local field survey precedence)
   - MANUAL_DISPUTE (Dispute escalation and locking)
6. Mixed batch reconciliation, retry backoff, and acknowledgment workflows.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
import pytest

from backend.edge.sync import (
    ChecksumMismatchError,
    ConflictRecord,
    ConflictResolutionPolicy,
    OfflineSyncQueue,
    ServerEntityRecord,
    SyncBatchResult,
    SyncCoordinator,
    SyncItemStatus,
    SyncOperationType,
    SyncQueueItem,
    compute_payload_checksum,
)


@pytest.fixture
def queue() -> OfflineSyncQueue:
    """Isolated local offline sync queue."""
    q = OfflineSyncQueue(worker_id="edge-pune-node-1")
    q.clear()
    return q


@pytest.fixture
def coordinator() -> SyncCoordinator:
    """Central sync coordinator."""
    return SyncCoordinator()


# =====================================================================
# 1. Offline Sync Queue Tests
# =====================================================================

class TestOfflineSyncQueue:
    """Tests for edge worker local FIFO transaction queue."""

    def test_enqueue_single_item(self, queue: OfflineSyncQueue):
        payload = {
            "parcel_identifier": "KHASRA-101",
            "khasra_number": "101",
            "owner": "Rajesh Sharma",
            "area_sqm": 2000.0,
            "coordinates": [[[0, 0], [40, 0], [40, 50], [0, 50], [0, 0]]],
        }

        item = queue.enqueue(
            entity_type="PARCEL",
            entity_id="KHASRA-101",
            operation_type=SyncOperationType.INSERT_PARCEL,
            base_version=0,
            payload=payload,
        )

        assert isinstance(item, SyncQueueItem)
        assert item.worker_id == "edge-pune-node-1"
        assert item.entity_id == "KHASRA-101"
        assert item.sequence_number == 1
        assert item.status == SyncItemStatus.QUEUED
        assert item.checksum == compute_payload_checksum(payload)
        assert item.verify_checksum() is True
        assert queue.size() == 1

    def test_enqueue_monotonic_sequencing(self, queue: OfflineSyncQueue):
        for i in range(1, 6):
            queue.enqueue(
                entity_type="PARCEL",
                entity_id=f"KHASRA-{100 + i}",
                operation_type=SyncOperationType.UPDATE_PARCEL,
                base_version=1,
                payload={"khasra": str(100 + i)},
            )

        assert queue.size() == 5
        batch = queue.peek_batch(limit=10)
        assert len(batch) == 5
        assert [item.sequence_number for item in batch] == [1, 2, 3, 4, 5]

    def test_idempotent_enqueue_deduplication(self, queue: OfflineSyncQueue):
        payload = {"khasra": "101"}
        idempotency_key = "client-tx-uuid-101"

        item1 = queue.enqueue(
            entity_type="PARCEL",
            entity_id="KHASRA-101",
            operation_type=SyncOperationType.INSERT_PARCEL,
            base_version=0,
            payload=payload,
            idempotency_key=idempotency_key,
        )

        # Enqueue identical mutation with same idempotency key
        item2 = queue.enqueue(
            entity_type="PARCEL",
            entity_id="KHASRA-101",
            operation_type=SyncOperationType.INSERT_PARCEL,
            base_version=0,
            payload=payload,
            idempotency_key=idempotency_key,
        )

        assert item1.operation_id == item2.operation_id
        assert queue.size() == 1

    def test_peek_batch_limit(self, queue: OfflineSyncQueue):
        for i in range(10):
            queue.enqueue(
                entity_type="PARCEL",
                entity_id=f"KHASRA-{i}",
                operation_type=SyncOperationType.UPDATE_PARCEL,
                base_version=1,
                payload={"val": i},
            )

        batch = queue.peek_batch(limit=4)
        assert len(batch) == 4
        assert [b.sequence_number for b in batch] == [1, 2, 3, 4]
        # Peek does not remove from queue
        assert queue.size() == 10

    def test_mark_in_flight_and_acknowledge(self, queue: OfflineSyncQueue):
        item1 = queue.enqueue("PARCEL", "K1", SyncOperationType.INSERT_PARCEL, 0, {"a": 1})
        item2 = queue.enqueue("PARCEL", "K2", SyncOperationType.INSERT_PARCEL, 0, {"b": 2})

        queue.mark_in_flight([item1.operation_id])
        assert item1.status == SyncItemStatus.IN_FLIGHT
        assert item2.status == SyncItemStatus.QUEUED

        # Acknowledge item1 committed
        ack_count = queue.acknowledge_committed([item1.operation_id])
        assert ack_count == 1
        assert queue.size() == 1
        assert queue.peek_batch()[0].entity_id == "K2"

    def test_mark_failed_and_retry_backoff(self, queue: OfflineSyncQueue):
        item = queue.enqueue("PARCEL", "K1", SyncOperationType.UPDATE_PARCEL, 1, {"c": 3})

        # Failure 1
        queue.mark_failed(item.operation_id, error="Network timeout", max_retries=3)
        assert item.retry_count == 1
        assert item.status == SyncItemStatus.RETRYING

        # Failure 2
        queue.mark_failed(item.operation_id, error="Connection reset", max_retries=3)
        assert item.retry_count == 2
        assert item.status == SyncItemStatus.RETRYING

        # Failure 3 -> exceeds max_retries
        queue.mark_failed(item.operation_id, error="Service unavailable", max_retries=3)
        assert item.retry_count == 3
        assert item.status == SyncItemStatus.FAILED
        assert item.error_message == "Service unavailable"

    def test_checksum_tamper_detection(self, queue: OfflineSyncQueue):
        payload = {"owner": "Suresh Patel", "area": 1500}
        item = queue.enqueue("PARCEL", "K103", SyncOperationType.UPDATE_PARCEL, 1, payload)

        assert item.verify_checksum() is True

        # Maliciously modify payload without updating checksum
        item.payload["area"] = 99999
        assert item.verify_checksum() is False


# =====================================================================
# 2. Sync Coordinator Conflict Detection Tests
# =====================================================================

class TestSyncCoordinatorConflictDetection:
    """Tests for server-side reconciliation and collision detection."""

    def test_clean_insert_new_entity(self, coordinator: SyncCoordinator):
        now = datetime.now(timezone.utc)
        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="KHASRA-201",
            operation_type=SyncOperationType.INSERT_PARCEL,
            sequence_number=1,
            base_version=0,
            payload={"khasra": "201", "owner": "Anita Singh"},
            client_timestamp=now,
        )

        result = coordinator.reconcile_batch([item])

        assert isinstance(result, SyncBatchResult)
        assert result.total_items == 1
        assert result.committed_count == 1
        assert result.conflict_count == 0
        assert item.operation_id in result.acknowledged_operation_ids

        # Server record is created
        server_rec = coordinator.get_server_entity("KHASRA-201")
        assert server_rec is not None
        assert server_rec.version == 1
        assert server_rec.payload["owner"] == "Anita Singh"

    def test_clean_monotonic_update(self, coordinator: SyncCoordinator):
        # Server currently at version 1
        coordinator.set_server_entity(
            entity_id="KHASRA-201",
            version=1,
            payload={"khasra": "201", "owner": "Anita Singh"},
        )

        # Client took base_version 1 and submits update
        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="KHASRA-201",
            operation_type=SyncOperationType.UPDATE_PARCEL,
            sequence_number=2,
            base_version=1,
            payload={"khasra": "201", "owner": "Anita Singh Verma"},
        )

        result = coordinator.reconcile_batch([item])

        assert result.committed_count == 1
        assert result.conflict_count == 0

        server_rec = coordinator.get_server_entity("KHASRA-201")
        assert server_rec.version == 2
        assert server_rec.payload["owner"] == "Anita Singh Verma"

    def test_version_drift_conflict_detection(self, coordinator: SyncCoordinator):
        # Server has evolved to version 3
        coordinator.set_server_entity(
            entity_id="KHASRA-201",
            version=3,
            payload={"khasra": "201", "owner": "Server Version 3"},
        )

        # Edge worker edited against stale base_version 1
        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="KHASRA-201",
            operation_type=SyncOperationType.UPDATE_PARCEL,
            sequence_number=1,
            base_version=1,
            payload={"khasra": "201", "owner": "Stale Client Edit"},
        )

        result = coordinator.reconcile_batch([item])

        assert result.conflict_count == 1
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.entity_id == "KHASRA-201"
        assert conflict.client_version == 1
        assert conflict.server_version == 3

    def test_corrupted_checksum_fails(self, coordinator: SyncCoordinator):
        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="KHASRA-201",
            operation_type=SyncOperationType.INSERT_PARCEL,
            sequence_number=1,
            base_version=0,
            payload={"khasra": "201"},
        )
        item.checksum = "corrupted_checksum_hash"

        result = coordinator.reconcile_batch([item])

        assert result.failed_count == 1
        assert result.committed_count == 0


# =====================================================================
# 3. Conflict Resolution Policy Tests
# =====================================================================

class TestConflictResolutionPolicies:
    """Tests evaluating deterministic arbitration strategies."""

    def test_last_write_wins_client_newer(self, coordinator: SyncCoordinator):
        t_server = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
        t_client = datetime(2026, 10, 10, 10, 30, 0, tzinfo=timezone.utc)  # Client is newer

        coordinator.set_server_entity(
            entity_id="P1",
            version=2,
            payload={"status": "SERVER_OLD"},
            updated_at=t_server,
        )

        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="P1",
            operation_type=SyncOperationType.UPDATE_PARCEL,
            sequence_number=1,
            base_version=1,
            payload={"status": "CLIENT_NEWER"},
            client_timestamp=t_client,
        )

        result = coordinator.reconcile_batch(
            [item], policy=ConflictResolutionPolicy.LAST_WRITE_WINS
        )

        assert result.conflict_count == 1
        assert item.operation_id in result.acknowledged_operation_ids

        # Client payload should have won
        server_rec = coordinator.get_server_entity("P1")
        assert server_rec.payload["status"] == "CLIENT_NEWER"
        assert server_rec.version == 3

    def test_last_write_wins_server_newer(self, coordinator: SyncCoordinator):
        t_client = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
        t_server = datetime(2026, 10, 10, 10, 30, 0, tzinfo=timezone.utc)  # Server is newer

        coordinator.set_server_entity(
            entity_id="P1",
            version=2,
            payload={"status": "SERVER_NEWER"},
            updated_at=t_server,
        )

        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="P1",
            operation_type=SyncOperationType.UPDATE_PARCEL,
            sequence_number=1,
            base_version=1,
            payload={"status": "CLIENT_OLD"},
            client_timestamp=t_client,
        )

        result = coordinator.reconcile_batch(
            [item], policy=ConflictResolutionPolicy.LAST_WRITE_WINS
        )

        assert result.conflict_count == 1
        # Server state preserved
        server_rec = coordinator.get_server_entity("P1")
        assert server_rec.payload["status"] == "SERVER_NEWER"
        assert server_rec.version == 2

    def test_coordinator_wins_policy(self, coordinator: SyncCoordinator):
        coordinator.set_server_entity(
            entity_id="P2",
            version=2,
            payload={"data": "AUTHORITATIVE_CENTRAL_DATA"},
        )

        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="P2",
            operation_type=SyncOperationType.UPDATE_PARCEL,
            sequence_number=1,
            base_version=1,
            payload={"data": "LOCAL_FIELD_DATA"},
        )

        result = coordinator.reconcile_batch(
            [item], policy=ConflictResolutionPolicy.COORDINATOR_WINS
        )

        assert result.conflict_count == 1
        server_rec = coordinator.get_server_entity("P2")
        assert server_rec.payload["data"] == "AUTHORITATIVE_CENTRAL_DATA"
        assert server_rec.version == 2

    def test_edge_wins_policy(self, coordinator: SyncCoordinator):
        coordinator.set_server_entity(
            entity_id="P3",
            version=2,
            payload={"data": "OLD_CENTRAL_DATA"},
        )

        item = SyncQueueItem.create(
            worker_id="edge-field-surv",
            entity_type="PARCEL",
            entity_id="P3",
            operation_type=SyncOperationType.UPDATE_PARCEL,
            sequence_number=1,
            base_version=1,
            payload={"data": "PRECISION_FIELD_SURVEY_DATA"},
        )

        result = coordinator.reconcile_batch(
            [item], policy=ConflictResolutionPolicy.EDGE_WINS
        )

        assert result.conflict_count == 1
        server_rec = coordinator.get_server_entity("P3")
        assert server_rec.payload["data"] == "PRECISION_FIELD_SURVEY_DATA"
        assert server_rec.version == 3

    def test_manual_dispute_policy(self, coordinator: SyncCoordinator):
        coordinator.set_server_entity(
            entity_id="KHASRA-DISPUTED",
            version=2,
            payload={"owner": "Ramesh", "dispute_locked": False},
        )

        item = SyncQueueItem.create(
            worker_id="edge-1",
            entity_type="PARCEL",
            entity_id="KHASRA-DISPUTED",
            operation_type=SyncOperationType.UPDATE_PARCEL,
            sequence_number=1,
            base_version=1,
            payload={"owner": "Suresh (Claimant)"},
        )

        result = coordinator.reconcile_batch(
            [item], policy=ConflictResolutionPolicy.MANUAL_DISPUTE
        )

        assert result.conflict_count == 1
        assert len(result.conflicts) == 1
        assert result.conflicts[0].is_disputed is True

        server_rec = coordinator.get_server_entity("KHASRA-DISPUTED")
        assert server_rec.is_disputed is True
        assert server_rec.payload["dispute_locked"] is True
        assert "conflicting_client_payload" in server_rec.payload


# =====================================================================
# 4. End-to-End Batch Synchronization Workflow
# =====================================================================

class TestBatchSynchronizationWorkflow:
    """Tests for complete offline collection, reconnect, and queue drainage."""

    def test_clean_offline_batch_synchronization(
        self, queue: OfflineSyncQueue, coordinator: SyncCoordinator
    ):
        # 1. Enqueue 3 mutations while offline
        item1 = queue.enqueue("PARCEL", "K101", SyncOperationType.INSERT_PARCEL, 0, {"k": "101"})
        item2 = queue.enqueue("PARCEL", "K102", SyncOperationType.INSERT_PARCEL, 0, {"k": "102"})
        item3 = queue.enqueue("PARCEL", "K103", SyncOperationType.INSERT_PARCEL, 0, {"k": "103"})

        assert queue.size() == 3

        # 2. Node reconnects: peek batch & mark in flight
        batch = queue.peek_batch()
        queue.mark_in_flight([b.operation_id for b in batch])

        # 3. Transmit to coordinator
        result = coordinator.reconcile_batch(batch)
        assert result.committed_count == 3
        assert result.conflict_count == 0

        # 4. Client receives acknowledgment and clears queue
        cleared = queue.acknowledge_committed(result.acknowledged_operation_ids)
        assert cleared == 3
        assert queue.size() == 0

    def test_mixed_batch_with_clean_and_conflicting_updates(
        self, queue: OfflineSyncQueue, coordinator: SyncCoordinator
    ):
        # Server has P1 at v1, and P2 evolved to v2
        coordinator.set_server_entity("P1", version=1, payload={"name": "P1 v1"})
        coordinator.set_server_entity("P2", version=2, payload={"name": "P2 v2 (evolved)"})

        # Client enqueues clean update for P1 (base_version 1)
        queue.enqueue("PARCEL", "P1", SyncOperationType.UPDATE_PARCEL, 1, {"name": "P1 v2"})

        # Client enqueues conflicting update for P2 (base_version 1 < server v2)
        queue.enqueue("PARCEL", "P2", SyncOperationType.UPDATE_PARCEL, 1, {"name": "P2 stale"})

        batch = queue.peek_batch()
        result = coordinator.reconcile_batch(
            batch, policy=ConflictResolutionPolicy.COORDINATOR_WINS
        )

        assert result.total_items == 2
        assert result.committed_count == 1   # P1 clean commit
        assert result.conflict_count == 1    # P2 conflict detected
        assert len(result.conflicts) == 1

        # Both acknowledged to clear client queue under Coordinator Wins
        cleared = queue.acknowledge_committed(result.acknowledged_operation_ids)
        assert cleared == 2
        assert queue.size() == 0

    def test_idempotent_retransmission(
        self, queue: OfflineSyncQueue, coordinator: SyncCoordinator
    ):
        item = queue.enqueue(
            "PARCEL",
            "P100",
            SyncOperationType.INSERT_PARCEL,
            0,
            {"khasra": "100"},
            idempotency_key="tx-unique-key-100",
        )

        # First sync transmission
        res1 = coordinator.reconcile_batch([item])
        assert res1.committed_count == 1
        assert item.operation_id in res1.acknowledged_operation_ids

        # Re-transmit same item (e.g. network timeout on receipt of ack)
        res2 = coordinator.reconcile_batch([item])
        assert res2.committed_count == 1
        assert item.operation_id in res2.acknowledged_operation_ids
        # Server version does not double-increment
        server_rec = coordinator.get_server_entity("P100")
        assert server_rec.version == 1
