"""Edge-to-cloud delta synchronization engine with compression and bandwidth throttling.

Enables:
1. Cadastral state diffing with fine-grained field-level change tracking (ADD, MODIFY, DELETE).
2. Pluggable delta compression (GZIP, ZLIB, NONE) with compression ratio instrumentation.
3. Adaptive token-bucket bandwidth throttling configured by cellular network tier (2G, 3G, 4G).
4. Deterministic chunked transmission with SHA-256 integrity verification and reassembly.
5. End-to-end edge-to-cloud package packaging and application.
"""

from __future__ import annotations

import copy
import enum
import gzip
import hashlib
import json
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


class DeltaOperationType(str, enum.Enum):
    """Classification of state mutation in a delta record."""

    ADD = "ADD"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


class DeltaCompressionType(str, enum.Enum):
    """Supported compression schemes for delta payloads."""

    NONE = "NONE"
    GZIP = "GZIP"
    ZLIB = "ZLIB"


class NetworkTier(str, enum.Enum):
    """Standardized connectivity profiles for rural edge deployment."""

    TIER_2G = "2G"         # 16 KB/s
    TIER_3G = "3G"         # 128 KB/s
    TIER_4G = "4G"         # 1024 KB/s (1 MB/s)
    UNLIMITED = "UNLIMITED"  # No rate limit


NETWORK_TIER_BYTE_RATES: Dict[NetworkTier, int] = {
    NetworkTier.TIER_2G: 16 * 1024,
    NetworkTier.TIER_3G: 128 * 1024,
    NetworkTier.TIER_4G: 1024 * 1024,
    NetworkTier.UNLIMITED: 100 * 1024 * 1024,
}


@dataclass
class DeltaRecord:
    """Individual entity mutation record inside a delta."""

    entity_id: str
    operation: DeltaOperationType
    old_data: Optional[Dict[str, Any]] = None
    new_data: Optional[Dict[str, Any]] = None
    field_diffs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "operation": self.operation.value,
            "old_data": self.old_data,
            "new_data": self.new_data,
            "field_diffs": self.field_diffs,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DeltaRecord:
        return cls(
            entity_id=data["entity_id"],
            operation=DeltaOperationType(data["operation"]),
            old_data=data.get("old_data"),
            new_data=data.get("new_data"),
            field_diffs=data.get("field_diffs", {}),
        )


@dataclass
class CadastralDelta:
    """Consolidated state delta between two cadastral revisions."""

    delta_id: str
    source_worker_id: str
    created_at: datetime
    added: List[DeltaRecord] = field(default_factory=list)
    modified: List[DeltaRecord] = field(default_factory=list)
    deleted: List[DeltaRecord] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)

    @property
    def is_empty(self) -> bool:
        return self.total_changes == 0

    def apply_to(self, base_state: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Reconstitute target state by applying delta mutations over a base dictionary."""
        result = copy.deepcopy(base_state)
        # 1. Apply Deletions
        for rec in self.deleted:
            result.pop(rec.entity_id, None)

        # 2. Apply Modifications
        for rec in self.modified:
            if rec.new_data is not None:
                result[rec.entity_id] = copy.deepcopy(rec.new_data)

        # 3. Apply Additions
        for rec in self.added:
            if rec.new_data is not None:
                result[rec.entity_id] = copy.deepcopy(rec.new_data)

        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "source_worker_id": self.source_worker_id,
            "created_at": self.created_at.isoformat(),
            "added": [r.to_dict() for r in self.added],
            "modified": [r.to_dict() for r in self.modified],
            "deleted": [r.to_dict() for r in self.deleted],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> CadastralDelta:
        return cls(
            delta_id=d["delta_id"],
            source_worker_id=d.get("source_worker_id", "unknown"),
            created_at=datetime.fromisoformat(d["created_at"]),
            added=[DeltaRecord.from_dict(r) for r in d.get("added", [])],
            modified=[DeltaRecord.from_dict(r) for r in d.get("modified", [])],
            deleted=[DeltaRecord.from_dict(r) for r in d.get("deleted", [])],
        )


def compute_cadastral_delta(
    base_state: Dict[str, Dict[str, Any]],
    target_state: Dict[str, Dict[str, Any]],
    source_worker_id: str = "edge-node",
) -> CadastralDelta:
    """Compute deterministic delta difference between base and target cadastral states."""
    delta_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    base_keys = set(base_state.keys())
    target_keys = set(target_state.keys())

    added: List[DeltaRecord] = []
    modified: List[DeltaRecord] = []
    deleted: List[DeltaRecord] = []

    # 1. Added keys
    for k in sorted(target_keys - base_keys):
        added.append(
            DeltaRecord(
                entity_id=k,
                operation=DeltaOperationType.ADD,
                new_data=copy.deepcopy(target_state[k]),
            )
        )

    # 2. Deleted keys
    for k in sorted(base_keys - target_keys):
        deleted.append(
            DeltaRecord(
                entity_id=k,
                operation=DeltaOperationType.DELETE,
                old_data=copy.deepcopy(base_state[k]),
            )
        )

    # 3. Potential modifications
    for k in sorted(base_keys & target_keys):
        b_val = base_state[k]
        t_val = target_state[k]
        if b_val != t_val:
            # Calculate field-level differences
            all_fields = set(b_val.keys()) | set(t_val.keys())
            diffs: Dict[str, Dict[str, Any]] = {}
            for f in sorted(all_fields):
                v_old = b_val.get(f)
                v_new = t_val.get(f)
                if v_old != v_new:
                    diffs[f] = {"old": v_old, "new": v_new}

            modified.append(
                DeltaRecord(
                    entity_id=k,
                    operation=DeltaOperationType.MODIFY,
                    old_data=copy.deepcopy(b_val),
                    new_data=copy.deepcopy(t_val),
                    field_diffs=diffs,
                )
            )

    return CadastralDelta(
        delta_id=delta_id,
        source_worker_id=source_worker_id,
        created_at=now,
        added=added,
        modified=modified,
        deleted=deleted,
    )


class DeltaCompressor:
    """Compress and decompress cadastral deltas across diverse formats."""

    def __init__(
        self,
        compression_type: DeltaCompressionType = DeltaCompressionType.GZIP,
        compression_level: int = 6,
    ) -> None:
        self.compression_type = compression_type
        self.compression_level = compression_level
        self._last_stats: Dict[str, Any] = {
            "original_bytes": 0,
            "compressed_bytes": 0,
            "compression_ratio": 1.0,
        }

    def compress(self, delta: CadastralDelta) -> bytes:
        """Serialize delta to canonical JSON and compress according to configured codec."""
        payload_dict = delta.to_dict()
        serialized = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        orig_len = len(serialized)

        if self.compression_type == DeltaCompressionType.GZIP:
            compressed = gzip.compress(serialized, compresslevel=self.compression_level)
        elif self.compression_type == DeltaCompressionType.ZLIB:
            compressed = zlib.compress(serialized, level=self.compression_level)
        else:
            compressed = serialized

        comp_len = len(compressed)
        ratio = orig_len / comp_len if comp_len > 0 else 1.0

        self._last_stats = {
            "original_bytes": orig_len,
            "compressed_bytes": comp_len,
            "compression_ratio": ratio,
        }
        return compressed

    def decompress(self, data: bytes) -> CadastralDelta:
        """Decompress bytes and deserialize into CadastralDelta."""
        if self.compression_type == DeltaCompressionType.GZIP:
            raw = gzip.decompress(data)
        elif self.compression_type == DeltaCompressionType.ZLIB:
            raw = zlib.decompress(data)
        else:
            raw = data

        parsed = json.loads(raw.decode("utf-8"))
        return CadastralDelta.from_dict(parsed)

    def get_last_stats(self) -> Dict[str, Any]:
        return dict(self._last_stats)


@dataclass
class ThrottledTransmissionResult:
    """Outcome of throttled data transmission simulation."""

    bytes_transmitted: int
    wait_time_sec: float
    actual_speed_bps: float
    throttled: bool


class BandwidthThrottler:
    """Token-bucket rate limiter enforcing bandwidth ceilings and modeling latency."""

    def __init__(
        self,
        max_bytes_per_sec: int,
        burst_capacity: Optional[int] = None,
        current_tier: NetworkTier = NetworkTier.UNLIMITED,
    ) -> None:
        self.max_bytes_per_sec = max_bytes_per_sec
        self.burst_capacity = burst_capacity if burst_capacity is not None else max_bytes_per_sec
        self.current_tier = current_tier

        self.available_tokens = float(self.burst_capacity)
        self.last_refill_timestamp = time.monotonic()
        self._lock = threading.RLock()

    @classmethod
    def from_network_tier(cls, tier: NetworkTier) -> BandwidthThrottler:
        rate = NETWORK_TIER_BYTE_RATES[tier]
        return cls(max_bytes_per_sec=rate, burst_capacity=rate, current_tier=tier)

    def set_network_tier(self, tier: NetworkTier) -> None:
        with self._lock:
            self.current_tier = tier
            rate = NETWORK_TIER_BYTE_RATES[tier]
            self.max_bytes_per_sec = rate
            self.burst_capacity = rate
            self.available_tokens = min(self.available_tokens, float(rate))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill_timestamp
        if elapsed > 0:
            tokens_to_add = elapsed * self.max_bytes_per_sec
            self.available_tokens = min(float(self.burst_capacity), self.available_tokens + tokens_to_add)
            self.last_refill_timestamp = now

    def acquire(self, num_bytes: int) -> float:
        """Acquire tokens for num_bytes. Returns wait time in seconds if throttled, or 0.0."""
        with self._lock:
            self._refill()
            if self.available_tokens >= num_bytes:
                self.available_tokens -= num_bytes
                return 0.0

            deficit = num_bytes - self.available_tokens
            wait_time = deficit / self.max_bytes_per_sec
            # Consume tokens down into deficit
            self.available_tokens -= num_bytes
            return wait_time

    def simulate_transmission(self, data: bytes) -> ThrottledTransmissionResult:
        """Simulate transmitting bytes through throttler with token consumption."""
        total_len = len(data)
        wait_time = self.acquire(total_len)
        throttled = wait_time > 0.0

        effective_time = max(0.001, wait_time + (total_len / self.max_bytes_per_sec))
        speed_bps = total_len / effective_time

        return ThrottledTransmissionResult(
            bytes_transmitted=total_len,
            wait_time_sec=wait_time,
            actual_speed_bps=speed_bps,
            throttled=throttled,
        )


@dataclass
class DeltaChunk:
    """Individual chunk in a split delta transmission."""

    delta_id: str
    chunk_index: int
    total_chunks: int
    chunk_data: bytes
    checksum: str

    @classmethod
    def create(cls, delta_id: str, chunk_index: int, total_chunks: int, chunk_data: bytes) -> DeltaChunk:
        ck = hashlib.sha256(chunk_data).hexdigest()
        return cls(
            delta_id=delta_id,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            chunk_data=chunk_data,
            checksum=ck,
        )

    def verify_checksum(self) -> bool:
        return hashlib.sha256(self.chunk_data).hexdigest() == self.checksum

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "checksum": self.checksum,
            "size_bytes": len(self.chunk_data),
        }


class DeltaChunkManager:
    """Handles splitting compressed binary deltas into chunks and safe reassembly."""

    def __init__(self, chunk_size_bytes: int = 64 * 1024) -> None:
        self.chunk_size_bytes = chunk_size_bytes

    def split_into_chunks(self, data: bytes, delta_id: str) -> List[DeltaChunk]:
        """Split data into indexed DeltaChunk instances."""
        if not data:
            return []

        chunks: List[DeltaChunk] = []
        total_len = len(data)
        total_chunks = (total_len + self.chunk_size_bytes - 1) // self.chunk_size_bytes

        for idx in range(total_chunks):
            start = idx * self.chunk_size_bytes
            end = min(start + self.chunk_size_bytes, total_len)
            part = data[start:end]
            chunks.append(DeltaChunk.create(delta_id, idx, total_chunks, part))

        return chunks

    def reassemble(self, chunks: Sequence[DeltaChunk]) -> bytes:
        """Reassemble chunks into unified byte sequence with ordering and checksum checks."""
        if not chunks:
            return b""

        total_expected = chunks[0].total_chunks
        delta_id = chunks[0].delta_id

        # Index verification
        chunk_map: Dict[int, DeltaChunk] = {}
        for ch in chunks:
            if ch.delta_id != delta_id:
                raise ValueError(f"Mismatched delta_id in chunk: expected '{delta_id}', got '{ch.delta_id}'.")
            if not ch.verify_checksum():
                raise ValueError(f"Checksum mismatch on chunk {ch.chunk_index} for delta '{delta_id}'.")
            chunk_map[ch.chunk_index] = ch

        if len(chunk_map) != total_expected:
            missing = [i for i in range(total_expected) if i not in chunk_map]
            raise ValueError(f"Missing chunks for delta '{delta_id}': {missing}")

        assembled = bytearray()
        for idx in range(total_expected):
            assembled.extend(chunk_map[idx].chunk_data)

        return bytes(assembled)


@dataclass
class SyncPackage:
    """Encapsulated synchronization transmission bundle."""

    delta_id: str
    is_empty: bool
    total_chunks: int
    compression: DeltaCompressionType
    uncompressed_size: int
    compressed_size: int
    chunks: List[DeltaChunk]


class DeltaSyncEngine:
    """Coordinator engine orchestrating delta generation, compression, throttling, and transmission."""

    def __init__(
        self,
        worker_id: str = "edge-worker",
        network_tier: NetworkTier = NetworkTier.TIER_3G,
        compression: DeltaCompressionType = DeltaCompressionType.GZIP,
        chunk_size_bytes: int = 64 * 1024,
    ) -> None:
        self.worker_id = worker_id
        self.network_tier = network_tier
        self.compression = compression
        self.chunk_size_bytes = chunk_size_bytes

        self.compressor = DeltaCompressor(compression_type=compression)
        self.throttler = BandwidthThrottler.from_network_tier(network_tier)
        self.chunk_manager = DeltaChunkManager(chunk_size_bytes=chunk_size_bytes)

    def prepare_sync_package(
        self,
        base_state: Dict[str, Dict[str, Any]],
        current_state: Dict[str, Dict[str, Any]],
    ) -> SyncPackage:
        """Compute delta between base and current state, compress, and split into chunks."""
        delta = compute_cadastral_delta(
            base_state=base_state,
            target_state=current_state,
            source_worker_id=self.worker_id,
        )

        if delta.is_empty:
            return SyncPackage(
                delta_id=delta.delta_id,
                is_empty=True,
                total_chunks=0,
                compression=self.compression,
                uncompressed_size=0,
                compressed_size=0,
                chunks=[],
            )

        compressed_data = self.compressor.compress(delta)
        stats = self.compressor.get_last_stats()
        chunks = self.chunk_manager.split_into_chunks(compressed_data, delta_id=delta.delta_id)

        return SyncPackage(
            delta_id=delta.delta_id,
            is_empty=False,
            total_chunks=len(chunks),
            compression=self.compression,
            uncompressed_size=stats["original_bytes"],
            compressed_size=stats["compressed_bytes"],
            chunks=chunks,
        )

    def transmit_package(self, package: SyncPackage) -> Dict[str, Any]:
        """Simulate transmitting the chunks through the bandwidth throttler."""
        if package.is_empty:
            return {
                "success": True,
                "skipped_empty": True,
                "chunks_transmitted": 0,
                "total_bytes": 0,
                "elapsed_sec": 0.0,
            }

        total_bytes = 0
        total_wait_sec = 0.0

        for ch in package.chunks:
            res = self.throttler.simulate_transmission(ch.chunk_data)
            total_bytes += res.bytes_transmitted
            total_wait_sec += res.wait_time_sec

        return {
            "success": True,
            "skipped_empty": False,
            "delta_id": package.delta_id,
            "chunks_transmitted": len(package.chunks),
            "total_bytes": total_bytes,
            "total_wait_sec": total_wait_sec,
        }

    def receive_and_apply(
        self,
        package: SyncPackage,
        base_state: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Reassemble chunks on receiver side, decompress delta, and apply to base state."""
        if package.is_empty:
            return copy.deepcopy(base_state)

        reassembled_bytes = self.chunk_manager.reassemble(package.chunks)
        delta = self.compressor.decompress(reassembled_bytes)
        return delta.apply_to(base_state)
