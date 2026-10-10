"""Offline queue synchronization and conflict resolution engine for decentralized edge nodes.

Enables:
1. Local offline transaction enqueuing with cryptographic checksums and monotonic sequencing.
2. Batch transmission and reconciliation with central coordinator.
3. Version and timestamp based conflict detection across concurrent parcel updates.
4. Pluggable conflict resolution policies:
   - LAST_WRITE_WINS (LWW deterministic timestamp ordering)
   - COORDINATOR_WINS (Authoritative central record precedence)
   - EDGE_WINS (Local field measurement precedence)
   - MANUAL_DISPUTE (Dispute escalation locking parcel for revenue officer review)
"""

from __future__ import annotations

import copy
import enum
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class SyncOperationType(str, enum.Enum):
    """Classification of queued offline cadastral mutations."""

    INSERT_PARCEL = "INSERT_PARCEL"
    UPDATE_PARCEL = "UPDATE_PARCEL"
    DELETE_PARCEL = "DELETE_PARCEL"
    RESOLVE_DISPUTE = "RESOLVE_DISPUTE"
    BATCH_HARMONIZATION = "BATCH_HARMONIZATION"


class SyncItemStatus(str, enum.Enum):
    """Lifecycle state of an offline sync queue item."""

    QUEUED = "QUEUED"
    IN_FLIGHT = "IN_FLIGHT"
    COMMITTED = "COMMITTED"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class ConflictResolutionPolicy(str, enum.Enum):
    """Deterministic conflict arbitration policies."""

    LAST_WRITE_WINS = "LAST_WRITE_WINS"
    COORDINATOR_WINS = "COORDINATOR_WINS"
    EDGE_WINS = "EDGE_WINS"
    MANUAL_DISPUTE = "MANUAL_DISPUTE"


class SyncError(Exception):
    """Base exception for synchronization errors."""

    pass


class ChecksumMismatchError(SyncError):
    """Raised when sync item checksum does not match payload content."""

    pass


def compute_payload_checksum(payload: Dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of a JSON-serializable payload."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass
class SyncQueueItem:
    """Single cadastral mutation enqueued on an edge worker while offline."""

    operation_id: str
    worker_id: str
    entity_type: str  # e.g., "PARCEL", "LAYER"
    entity_id: str    # e.g., "KHASRA-101"
    operation_type: SyncOperationType
    sequence_number: int
    client_timestamp: datetime
    base_version: int   # Version of the entity when client began edit
    payload: Dict[str, Any]
    checksum: str
    idempotency_key: str
    status: SyncItemStatus = SyncItemStatus.QUEUED
    retry_count: int = 0
    error_message: Optional[str] = None

    @classmethod
    def create(
        cls,
        worker_id: str,
        entity_type: str,
        entity_id: str,
        operation_type: SyncOperationType,
        sequence_number: int,
        base_version: int,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        client_timestamp: Optional[datetime] = None,
    ) -> SyncQueueItem:
        op_id = str(uuid.uuid4())
        ts = client_timestamp or datetime.now(timezone.utc)
        cksum = compute_payload_checksum(payload)
        ik = idempotency_key or f"{worker_id}:{entity_id}:{sequence_number}"
        return cls(
            operation_id=op_id,
            worker_id=worker_id,
            entity_type=entity_type,
            entity_id=entity_id,
            operation_type=operation_type,
            sequence_number=sequence_number,
            client_timestamp=ts,
            base_version=base_version,
            payload=payload,
            checksum=cksum,
            idempotency_key=ik,
            status=SyncItemStatus.QUEUED,
        )

    def verify_checksum(self) -> bool:
        """Verify payload integrity against computed checksum."""
        return compute_payload_checksum(self.payload) == self.checksum

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "worker_id": self.worker_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "operation_type": self.operation_type.value,
            "sequence_number": self.sequence_number,
            "client_timestamp": self.client_timestamp.isoformat(),
            "base_version": self.base_version,
            "payload": self.payload,
            "checksum": self.checksum,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "retry_count": self.retry_count,
            "error_message": self.error_message,
        }


@dataclass
class ConflictRecord:
    """Detailed audit record of an identified data collision and its resolution."""

    conflict_id: str
    entity_id: str
    operation_id: str
    worker_id: str
    client_version: int
    server_version: int
    policy_applied: ConflictResolutionPolicy
    resolved_version: int
    is_disputed: bool
    details: str
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "entity_id": self.entity_id,
            "operation_id": self.operation_id,
            "worker_id": self.worker_id,
            "client_version": self.client_version,
            "server_version": self.server_version,
            "policy_applied": self.policy_applied.value,
            "resolved_version": self.resolved_version,
            "is_disputed": self.is_disputed,
            "details": self.details,
            "resolved_at": self.resolved_at.isoformat(),
        }


@dataclass
class SyncBatchResult:
    """Outcome of reconciling a batch of sync items at the central coordinator."""

    batch_id: str
    total_items: int
    committed_count: int
    conflict_count: int
    failed_count: int
    conflicts: List[ConflictRecord] = field(default_factory=list)
    acknowledged_operation_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "total_items": self.total_items,
            "committed_count": self.committed_count,
            "conflict_count": self.conflict_count,
            "failed_count": self.failed_count,
            "acknowledged_operation_ids": list(self.acknowledged_operation_ids),
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


class OfflineSyncQueue:
    """Local FIFO queue operating on an edge worker with persistent sequencing and deduplication."""

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self._queue: List[SyncQueueItem] = []
        self._idempotency_keys: Set[str] = set()
        self._current_sequence = 0
        self._lock = threading.RLock()

    def clear(self) -> None:
        """Reset the queue (for test teardown)."""
        with self._lock:
            self._queue.clear()
            self._idempotency_keys.clear()
            self._current_sequence = 0

    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def enqueue(
        self,
        entity_type: str,
        entity_id: str,
        operation_type: SyncOperationType,
        base_version: int,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        client_timestamp: Optional[datetime] = None,
    ) -> SyncQueueItem:
        """Enqueue a new mutation. Idempotent: returns existing item if key already enqueued."""
        with self._lock:
            ik = idempotency_key or f"{self.worker_id}:{entity_id}:{self._current_sequence + 1}"
            if ik in self._idempotency_keys:
                # Return existing queued item
                for item in self._queue:
                    if item.idempotency_key == ik:
                        return item

            self._current_sequence += 1
            item = SyncQueueItem.create(
                worker_id=self.worker_id,
                entity_type=entity_type,
                entity_id=entity_id,
                operation_type=operation_type,
                sequence_number=self._current_sequence,
                base_version=base_version,
                payload=payload,
                idempotency_key=ik,
                client_timestamp=client_timestamp,
            )
            self._queue.append(item)
            self._idempotency_keys.add(ik)
            return item

    def peek_batch(self, limit: int = 50) -> List[SyncQueueItem]:
        """View pending queued items without removing them."""
        with self._lock:
            pending = [item for item in self._queue if item.status in (SyncItemStatus.QUEUED, SyncItemStatus.RETRYING)]
            return pending[:limit]

    def mark_in_flight(self, operation_ids: Sequence[str]) -> None:
        """Mark items as in-flight during transmission."""
        with self._lock:
            id_set = set(operation_ids)
            for item in self._queue:
                if item.operation_id in id_set:
                    item.status = SyncItemStatus.IN_FLIGHT

    def acknowledge_committed(self, operation_ids: Sequence[str]) -> int:
        """Acknowledge successfully reconciled items and remove from queue."""
        with self._lock:
            id_set = set(operation_ids)
            initial_len = len(self._queue)
            self._queue = [item for item in self._queue if item.operation_id not in id_set]
            # Clean idempotency keys
            removed_count = initial_len - len(self._queue)
            return removed_count

    def mark_failed(self, operation_id: str, error: str, max_retries: int = 3) -> None:
        """Mark an item as failed, incrementing retry counter."""
        with self._lock:
            for item in self._queue:
                if item.operation_id == operation_id:
                    item.retry_count += 1
                    item.error_message = error
                    if item.retry_count >= max_retries:
                        item.status = SyncItemStatus.FAILED
                    else:
                        item.status = SyncItemStatus.RETRYING
                    break


class ServerEntityRecord:
    """Server-side representation of an authoritative cadastral entity."""

    def __init__(
        self,
        entity_id: str,
        version: int = 1,
        payload: Optional[Dict[str, Any]] = None,
        updated_at: Optional[datetime] = None,
        is_disputed: bool = False,
    ) -> None:
        self.entity_id = entity_id
        self.version = version
        self.payload = payload or {}
        self.updated_at = updated_at or datetime.now(timezone.utc)
        self.is_disputed = is_disputed


class SyncCoordinator:
    """Central server coordinator reconciling sync batches from edge workers."""

    def __init__(
        self,
        default_policy: ConflictResolutionPolicy = ConflictResolutionPolicy.LAST_WRITE_WINS,
    ) -> None:
        self.default_policy = default_policy
        self._server_store: Dict[str, ServerEntityRecord] = {}
        self._processed_idempotency_keys: Set[str] = set()
        self._lock = threading.RLock()

    def set_server_entity(
        self,
        entity_id: str,
        version: int,
        payload: Dict[str, Any],
        updated_at: Optional[datetime] = None,
        is_disputed: bool = False,
    ) -> ServerEntityRecord:
        """Seed or update authoritative server entity state."""
        with self._lock:
            rec = ServerEntityRecord(
                entity_id=entity_id,
                version=version,
                payload=payload,
                updated_at=updated_at or datetime.now(timezone.utc),
                is_disputed=is_disputed,
            )
            self._server_store[entity_id] = rec
            return rec

    def get_server_entity(self, entity_id: str) -> Optional[ServerEntityRecord]:
        with self._lock:
            return self._server_store.get(entity_id)

    def reconcile_batch(
        self,
        items: Sequence[SyncQueueItem],
        policy: Optional[ConflictResolutionPolicy] = None,
    ) -> SyncBatchResult:
        """Reconcile a batch of sync items against the server store, detecting and resolving conflicts."""
        active_policy = policy or self.default_policy
        batch_id = str(uuid.uuid4())
        acknowledged_ids: List[str] = []
        conflicts: List[ConflictRecord] = []
        committed_count = 0
        conflict_count = 0
        failed_count = 0

        with self._lock:
            for item in items:
                # 1. Verify payload checksum integrity
                if not item.verify_checksum():
                    failed_count += 1
                    continue

                # 2. Idempotency deduplication
                if item.idempotency_key in self._processed_idempotency_keys:
                    acknowledged_ids.append(item.operation_id)
                    committed_count += 1
                    continue

                server_rec = self._server_store.get(item.entity_id)

                if server_rec is None:
                    # Brand new entity -> commit immediately
                    new_rec = ServerEntityRecord(
                        entity_id=item.entity_id,
                        version=max(1, item.base_version + 1),
                        payload=copy.deepcopy(item.payload),
                        updated_at=item.client_timestamp,
                        is_disputed=False,
                    )
                    self._server_store[item.entity_id] = new_rec
                    self._processed_idempotency_keys.add(item.idempotency_key)
                    acknowledged_ids.append(item.operation_id)
                    committed_count += 1
                    continue

                # 3. Check for version drift / collision
                if server_rec.version > item.base_version:
                    # Conflict detected: Server has evolved since client took its base snapshot
                    conflict_count += 1
                    conflict_rec = self._resolve_conflict(item, server_rec, active_policy)
                    conflicts.append(conflict_rec)

                    if active_policy == ConflictResolutionPolicy.EDGE_WINS:
                        # Edge overwrites server
                        server_rec.version += 1
                        server_rec.payload = copy.deepcopy(item.payload)
                        server_rec.updated_at = item.client_timestamp
                        acknowledged_ids.append(item.operation_id)
                    elif active_policy == ConflictResolutionPolicy.COORDINATOR_WINS:
                        # Server rejected client update; acknowledge to clear client queue
                        acknowledged_ids.append(item.operation_id)
                    elif active_policy == ConflictResolutionPolicy.LAST_WRITE_WINS:
                        # Compare timestamps
                        if item.client_timestamp > server_rec.updated_at:
                            server_rec.version += 1
                            server_rec.payload = copy.deepcopy(item.payload)
                            server_rec.updated_at = item.client_timestamp
                        acknowledged_ids.append(item.operation_id)
                    elif active_policy == ConflictResolutionPolicy.MANUAL_DISPUTE:
                        server_rec.is_disputed = True
                        server_rec.payload["dispute_locked"] = True
                        server_rec.payload["conflicting_client_payload"] = copy.deepcopy(item.payload)
                        # Do not acknowledge: keep in dispute state
                else:
                    # Clean monotonic update: base_version matches current server version
                    server_rec.version += 1
                    server_rec.payload = copy.deepcopy(item.payload)
                    server_rec.updated_at = item.client_timestamp
                    self._processed_idempotency_keys.add(item.idempotency_key)
                    acknowledged_ids.append(item.operation_id)
                    committed_count += 1

        return SyncBatchResult(
            batch_id=batch_id,
            total_items=len(items),
            committed_count=committed_count,
            conflict_count=conflict_count,
            failed_count=failed_count,
            conflicts=conflicts,
            acknowledged_operation_ids=acknowledged_ids,
        )

    def _resolve_conflict(
        self,
        item: SyncQueueItem,
        server_rec: ServerEntityRecord,
        policy: ConflictResolutionPolicy,
    ) -> ConflictRecord:
        """Arbitrate conflict according to selected policy."""
        cid = str(uuid.uuid4())
        is_disputed = False
        details = ""
        resolved_version = server_rec.version

        if policy == ConflictResolutionPolicy.LAST_WRITE_WINS:
            if item.client_timestamp > server_rec.updated_at:
                details = f"LWW: Client update ({item.client_timestamp.isoformat()}) was newer than Server ({server_rec.updated_at.isoformat()}). Edge payload applied."
                resolved_version = server_rec.version + 1
            else:
                details = f"LWW: Server update ({server_rec.updated_at.isoformat()}) was newer than Client ({item.client_timestamp.isoformat()}). Server state preserved."

        elif policy == ConflictResolutionPolicy.COORDINATOR_WINS:
            details = f"Coordinator Wins: Central authority retained version {server_rec.version}. Client edits rejected."

        elif policy == ConflictResolutionPolicy.EDGE_WINS:
            details = f"Edge Wins: Local field measurement from worker {item.worker_id} applied over server version {server_rec.version}."
            resolved_version = server_rec.version + 1

        elif policy == ConflictResolutionPolicy.MANUAL_DISPUTE:
            is_disputed = True
            details = f"Manual Dispute: Parcel {item.entity_id} locked with dispute flag for Tehsildar / Survey Officer manual review."

        return ConflictRecord(
            conflict_id=cid,
            entity_id=item.entity_id,
            operation_id=item.operation_id,
            worker_id=item.worker_id,
            client_version=item.base_version,
            server_version=server_rec.version,
            policy_applied=policy,
            resolved_version=resolved_version,
            is_disputed=is_disputed,
            details=details,
        )

    def resolve_manual_dispute(
        self,
        entity_id: str,
        resolved_payload: Dict[str, Any],
        resolution_method: str = "TEHSILDAR_SIGN_OFF",
        resolved_by: str = "OFFICER",
    ) -> ServerEntityRecord:
        """Manually resolve an escalated dispute on an entity, unlocking dispute flag."""
        with self._lock:
            server_rec = self._server_store.get(entity_id)
            if not server_rec:
                raise SyncError(f"Entity '{entity_id}' not found for dispute resolution.")

            server_rec.version += 1
            server_rec.payload = copy.deepcopy(resolved_payload)
            server_rec.payload["dispute_locked"] = False
            server_rec.payload["resolution_method"] = resolution_method
            server_rec.payload["resolved_by"] = resolved_by
            server_rec.is_disputed = False
            server_rec.updated_at = datetime.now(timezone.utc)
            return server_rec

    def clear(self) -> None:
        """Clear all stored entities and processed idempotency keys (for testing)."""
        with self._lock:
            self._server_store.clear()
            self._processed_idempotency_keys.clear()


# Global default sync coordinator instance
sync_coordinator = SyncCoordinator()

