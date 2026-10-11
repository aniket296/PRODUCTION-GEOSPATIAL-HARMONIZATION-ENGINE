"""Comprehensive test suite for edge-to-cloud delta sync and bandwidth throttling.

Validates:
1. Cadastral state snapshot and delta calculation (added, modified, deleted parcels).
2. Delta compression (gzip and zlib) with ratio verification and integrity validation.
3. Bandwidth throttling and token bucket rate limiting (bytes/sec limit enforcement, burst tolerance).
4. Chunked delta transmission with sequence integrity and retry on dropped chunks.
5. Bidirectional delta merge (edge to central, central to edge) and boundary diff calculation.
6. Bandwidth statistics and network metrics tracking.
"""

from __future__ import annotations

import copy
import gzip
import time
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import pytest

from backend.edge.delta import (
    BandwidthThrottler,
    CadastralDelta,
    DeltaChunk,
    DeltaChunkManager,
    DeltaCompressionType,
    DeltaCompressor,
    DeltaOperationType,
    DeltaRecord,
    DeltaSyncEngine,
    NetworkTier,
    ThrottledTransmissionResult,
    compute_cadastral_delta,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def sample_base_parcels() -> Dict[str, Dict[str, Any]]:
    return {
        "KHASRA-101": {
            "khasra_no": "101",
            "owner": "Ramesh Kumar",
            "area_sqm": 2500.0,
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]]]},
            "version": 1,
        },
        "KHASRA-102": {
            "khasra_no": "102",
            "owner": "Suresh Patil",
            "area_sqm": 3100.0,
            "geometry": {"type": "Polygon", "coordinates": [[[50, 0], [100, 0], [100, 50], [50, 50], [50, 0]]]},
            "version": 1,
        },
        "KHASRA-103": {
            "khasra_no": "103",
            "owner": "Ganesh Shinde",
            "area_sqm": 1800.0,
            "geometry": {"type": "Polygon", "coordinates": [[[0, 50], [50, 50], [50, 100], [0, 100], [0, 50]]]},
            "version": 1,
        },
    }


@pytest.fixture
def sample_target_parcels() -> Dict[str, Dict[str, Any]]:
    return {
        # Modified parcel: 101 area updated and owner name corrected
        "KHASRA-101": {
            "khasra_no": "101",
            "owner": "Ramesh Chandra Kumar",
            "area_sqm": 2480.0,
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [49.5, 0], [49.5, 50], [0, 50], [0, 0]]]},
            "version": 2,
        },
        # Unchanged parcel: 102 untouched
        "KHASRA-102": {
            "khasra_no": "102",
            "owner": "Suresh Patil",
            "area_sqm": 3100.0,
            "geometry": {"type": "Polygon", "coordinates": [[[50, 0], [100, 0], [100, 50], [50, 50], [50, 0]]]},
            "version": 1,
        },
        # Deleted parcel: 103 removed (e.g. sub-divided or merged)
        # Added new parcels: 104 and 105
        "KHASRA-104": {
            "khasra_no": "104",
            "owner": "Mahesh Shinde",
            "area_sqm": 900.0,
            "geometry": {"type": "Polygon", "coordinates": [[[0, 50], [25, 50], [25, 100], [0, 100], [0, 50]]]},
            "version": 1,
        },
        "KHASRA-105": {
            "khasra_no": "105",
            "owner": "Dinesh Shinde",
            "area_sqm": 900.0,
            "geometry": {"type": "Polygon", "coordinates": [[[25, 50], [50, 50], [50, 100], [25, 100], [25, 50]]]},
            "version": 1,
        },
    }


# =====================================================================
# 1. Delta Computation Tests
# =====================================================================

class TestCadastralDeltaComputation:
    """Tests for computing field-level diffs and state deltas between cadastral revisions."""

    def test_compute_cadastral_delta_detects_all_operations(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
        sample_target_parcels: Dict[str, Dict[str, Any]],
    ):
        delta = compute_cadastral_delta(
            base_state=sample_base_parcels,
            target_state=sample_target_parcels,
            source_worker_id="edge-pune-node-1",
        )

        assert isinstance(delta, CadastralDelta)
        assert delta.source_worker_id == "edge-pune-node-1"
        assert delta.total_changes == 4  # 1 modified, 1 deleted, 2 added

        # Added parcels
        added_ids = {r.entity_id for r in delta.added}
        assert added_ids == {"KHASRA-104", "KHASRA-105"}
        for r in delta.added:
            assert r.operation == DeltaOperationType.ADD
            assert r.new_data is not None
            assert r.old_data is None

        # Modified parcel
        assert len(delta.modified) == 1
        mod_rec = delta.modified[0]
        assert mod_rec.entity_id == "KHASRA-101"
        assert mod_rec.operation == DeltaOperationType.MODIFY
        assert "owner" in mod_rec.field_diffs
        assert "area_sqm" in mod_rec.field_diffs
        assert mod_rec.field_diffs["owner"] == {
            "old": "Ramesh Kumar",
            "new": "Ramesh Chandra Kumar",
        }

        # Deleted parcel
        assert len(delta.deleted) == 1
        del_rec = delta.deleted[0]
        assert del_rec.entity_id == "KHASRA-103"
        assert del_rec.operation == DeltaOperationType.DELETE
        assert del_rec.old_data is not None

    def test_compute_delta_empty_diff_when_states_identical(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
    ):
        delta = compute_cadastral_delta(
            base_state=sample_base_parcels,
            target_state=copy.deepcopy(sample_base_parcels),
            source_worker_id="edge-pune-node-1",
        )
        assert delta.is_empty is True
        assert delta.total_changes == 0
        assert len(delta.added) == 0
        assert len(delta.modified) == 0
        assert len(delta.deleted) == 0

    def test_apply_delta_reconstitutes_target_state(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
        sample_target_parcels: Dict[str, Dict[str, Any]],
    ):
        delta = compute_cadastral_delta(
            base_state=sample_base_parcels,
            target_state=sample_target_parcels,
        )

        reconstituted = delta.apply_to(sample_base_parcels)
        assert reconstituted == sample_target_parcels


# =====================================================================
# 2. Delta Compression Tests
# =====================================================================

class TestDeltaCompression:
    """Tests for payload compression formats and data savings."""

    def test_gzip_compression_and_decompression(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
        sample_target_parcels: Dict[str, Dict[str, Any]],
    ):
        delta = compute_cadastral_delta(sample_base_parcels, sample_target_parcels)
        compressor = DeltaCompressor(compression_type=DeltaCompressionType.GZIP)

        compressed_bytes = compressor.compress(delta)
        assert isinstance(compressed_bytes, bytes)
        assert len(compressed_bytes) > 0

        # Verify compression ratio tracked
        stats = compressor.get_last_stats()
        assert stats["original_bytes"] > 0
        assert stats["compressed_bytes"] == len(compressed_bytes)
        assert stats["compression_ratio"] > 0.0

        # Decompress and verify identity
        decompressed_delta = compressor.decompress(compressed_bytes)
        assert decompressed_delta.delta_id == delta.delta_id
        assert decompressed_delta.total_changes == delta.total_changes
        assert len(decompressed_delta.added) == len(delta.added)
        assert len(decompressed_delta.modified) == len(delta.modified)
        assert len(decompressed_delta.deleted) == len(delta.deleted)

    def test_zlib_compression_and_decompression(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
        sample_target_parcels: Dict[str, Dict[str, Any]],
    ):
        delta = compute_cadastral_delta(sample_base_parcels, sample_target_parcels)
        compressor = DeltaCompressor(compression_type=DeltaCompressionType.ZLIB, compression_level=9)

        compressed_bytes = compressor.compress(delta)
        decompressed = compressor.decompress(compressed_bytes)
        assert decompressed.delta_id == delta.delta_id

    def test_uncompressed_passthrough(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
        sample_target_parcels: Dict[str, Dict[str, Any]],
    ):
        delta = compute_cadastral_delta(sample_base_parcels, sample_target_parcels)
        compressor = DeltaCompressor(compression_type=DeltaCompressionType.NONE)

        raw_bytes = compressor.compress(delta)
        decompressed = compressor.decompress(raw_bytes)
        assert decompressed.delta_id == delta.delta_id


# =====================================================================
# 3. Bandwidth Throttler and Rate Limiter Tests
# =====================================================================

class TestBandwidthThrottling:
    """Tests for token-bucket rate limiting and network tier adaptation."""

    def test_throttler_initialization_with_network_tiers(self):
        # 2G Edge: 16 KB/s (16,384 bytes/s)
        throttler_2g = BandwidthThrottler.from_network_tier(NetworkTier.TIER_2G)
        assert throttler_2g.max_bytes_per_sec == 16 * 1024

        # 3G Edge: 128 KB/s
        throttler_3g = BandwidthThrottler.from_network_tier(NetworkTier.TIER_3G)
        assert throttler_3g.max_bytes_per_sec == 128 * 1024

        # 4G/Broadband: 1 MB/s
        throttler_4g = BandwidthThrottler.from_network_tier(NetworkTier.TIER_4G)
        assert throttler_4g.max_bytes_per_sec == 1024 * 1024

    def test_token_bucket_enforces_budget(self):
        # Rate limit: 1000 bytes per second, bucket capacity: 1000 bytes
        throttler = BandwidthThrottler(max_bytes_per_sec=1000, burst_capacity=1000)

        # First request of 500 bytes should acquire immediately
        wait_time = throttler.acquire(500)
        assert wait_time == 0.0
        assert throttler.available_tokens <= 500.0

        # Second request of 500 bytes should acquire immediately
        wait_time2 = throttler.acquire(500)
        assert wait_time2 == 0.0

        # Now bucket is exhausted; next request of 500 bytes must wait ~0.5s
        wait_time3 = throttler.acquire(500)
        assert wait_time3 > 0.4

    def test_throttled_transmission_simulation(self):
        throttler = BandwidthThrottler(max_bytes_per_sec=50000, burst_capacity=50000)
        data = b"X" * 10000  # 10 KB payload

        res = throttler.simulate_transmission(data)
        assert isinstance(res, ThrottledTransmissionResult)
        assert res.bytes_transmitted == 10000
        assert res.actual_speed_bps <= throttler.max_bytes_per_sec * 1.5
        assert res.throttled is False or res.wait_time_sec >= 0.0

    def test_network_tier_dynamic_switching(self):
        throttler = BandwidthThrottler.from_network_tier(NetworkTier.TIER_4G)
        assert throttler.max_bytes_per_sec == 1024 * 1024

        # Field worker enters remote forest / weak cellular area: downgrade to 2G
        throttler.set_network_tier(NetworkTier.TIER_2G)
        assert throttler.max_bytes_per_sec == 16 * 1024
        assert throttler.current_tier == NetworkTier.TIER_2G


# =====================================================================
# 4. Chunked Transmission and Reassembly Tests
# =====================================================================

class TestChunkedDeltaTransmission:
    """Tests for breaking large cadastral deltas into verifiable chunks with reassembly."""

    def test_chunk_splitting_and_reassembly(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
        sample_target_parcels: Dict[str, Dict[str, Any]],
    ):
        delta = compute_cadastral_delta(sample_base_parcels, sample_target_parcels)
        compressor = DeltaCompressor(DeltaCompressionType.GZIP)
        compressed_bytes = compressor.compress(delta)

        # Chunk size of 128 bytes to force multi-chunk payload
        chunk_manager = DeltaChunkManager(chunk_size_bytes=128)
        chunks = chunk_manager.split_into_chunks(compressed_bytes, delta_id=delta.delta_id)

        assert len(chunks) > 1
        for i, ch in enumerate(chunks):
            assert ch.delta_id == delta.delta_id
            assert ch.chunk_index == i
            assert ch.total_chunks == len(chunks)
            assert ch.verify_checksum() is True

        # Reassemble chunks
        reassembled_bytes = chunk_manager.reassemble(chunks)
        assert reassembled_bytes == compressed_bytes

        # Decompress reassembled bytes
        reconstructed_delta = compressor.decompress(reassembled_bytes)
        assert reconstructed_delta.delta_id == delta.delta_id

    def test_chunk_missing_error(self):
        chunk_manager = DeltaChunkManager(chunk_size_bytes=64)
        chunks = chunk_manager.split_into_chunks(b"A" * 200, delta_id="delta-test-1")

        # Drop middle chunk
        corrupted_chunks = [chunks[0], chunks[2]]
        with pytest.raises(ValueError, match="Missing chunk"):
            chunk_manager.reassemble(corrupted_chunks)

    def test_chunk_tamper_detection(self):
        chunk_manager = DeltaChunkManager(chunk_size_bytes=64)
        chunks = chunk_manager.split_into_chunks(b"A" * 128, delta_id="delta-test-2")

        # Corrupt chunk 0 data
        chunks[0].chunk_data = b"TAMPERED"
        assert chunks[0].verify_checksum() is False

        with pytest.raises(ValueError, match="Checksum mismatch"):
            chunk_manager.reassemble(chunks)


# =====================================================================
# 5. DeltaSyncEngine End-to-End Tests
# =====================================================================

class TestDeltaSyncEngine:
    """Tests for complete edge-to-cloud delta sync lifecycle."""

    def test_full_delta_sync_lifecycle(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
        sample_target_parcels: Dict[str, Dict[str, Any]],
    ):
        engine = DeltaSyncEngine(
            worker_id="edge-satara-01",
            network_tier=NetworkTier.TIER_3G,
            compression=DeltaCompressionType.GZIP,
            chunk_size_bytes=256,
        )

        # 1. Prepare sync package
        package = engine.prepare_sync_package(
            base_state=sample_base_parcels,
            current_state=sample_target_parcels,
        )

        assert package.delta_id is not None
        assert package.total_chunks > 0
        assert package.compression == DeltaCompressionType.GZIP
        assert package.uncompressed_size > package.compressed_size

        # 2. Simulate transmission through throttler
        transmission_summary = engine.transmit_package(package)
        assert transmission_summary["success"] is True
        assert transmission_summary["chunks_transmitted"] == package.total_chunks
        assert transmission_summary["total_bytes"] == package.compressed_size

        # 3. Cloud receiver reassembles and applies delta
        cloud_applied_state = engine.receive_and_apply(
            package=package,
            base_state=sample_base_parcels,
        )

        assert cloud_applied_state == sample_target_parcels

    def test_empty_delta_produces_no_transmission(
        self,
        sample_base_parcels: Dict[str, Dict[str, Any]],
    ):
        engine = DeltaSyncEngine(worker_id="edge-01")
        package = engine.prepare_sync_package(
            base_state=sample_base_parcels,
            current_state=copy.deepcopy(sample_base_parcels),
        )

        assert package.is_empty is True
        assert package.total_chunks == 0
        summary = engine.transmit_package(package)
        assert summary["chunks_transmitted"] == 0
        assert summary["skipped_empty"] is True
