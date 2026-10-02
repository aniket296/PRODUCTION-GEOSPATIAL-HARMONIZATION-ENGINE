"""Tests for FlatGeobuf binary header parsing, CRS identification, and spatial index reading."""

from pathlib import Path
import struct
import pytest

from backend.ingestion.flatgeobuf_reader import (
    FlatGeobufCorruptHeaderError,
    FlatGeobufEmptyFileError,
    FlatGeobufExtractionError,
    FlatGeobufHeader,
    FlatGeobufIngestionReader,
    FlatGeobufIngestionResult,
    FlatGeobufInvalidMagicError,
    build_synthetic_fgb_payload,
    parse_fgb_header,
)
from flatgeobuf.constants import magicbytes
from flatgeobuf.packedrtree import calc_tree_size


def test_build_and_parse_valid_synthetic_fgb_header():
    """Verify that a synthetic FlatGeobuf payload parses accurately."""
    payload = build_synthetic_fgb_payload(
        name="kalianpur_cadastre",
        features_count=25,
        index_node_size=16,
        crs_code=24378,
        crs_name="EPSG:24378",
        envelope=(3600000.0, 500000.0, 3600500.0, 500300.0),
        columns=[("khasra_no", 1), ("owner_name", 1), ("area_sqm", 2)],
    )

    header = parse_fgb_header(payload)
    assert isinstance(header, FlatGeobufHeader)
    assert header.name == "kalianpur_cadastre"
    assert header.features_count == 25
    assert header.index_node_size == 16
    assert header.has_spatial_index is True
    assert header.crs_code == 24378
    assert header.canonical_crs == "EPSG:24378"
    assert header.envelope == (3600000.0, 500000.0, 3600500.0, 500300.0)
    assert header.columns == ["khasra_no", "owner_name", "area_sqm"]
    assert header.header_length > 0
    assert header.index_length == calc_tree_size(25, 16)


def test_invalid_magic_bytes_raises_error():
    """Verify that invalid magic bytes raise FlatGeobufInvalidMagicError."""
    # Build payload and corrupt magic bytes
    valid_payload = build_synthetic_fgb_payload()
    corrupt_magic_payload = b"BAD\x03fgb\x00" + valid_payload[8:]

    with pytest.raises(FlatGeobufInvalidMagicError) as exc_info:
        parse_fgb_header(corrupt_magic_payload)
    assert "Invalid FlatGeobuf magic bytes" in str(exc_info.value)


def test_truncated_payload_raises_corrupt_header_error():
    """Verify that truncated data buffers raise FlatGeobufCorruptHeaderError."""
    # Under 12 bytes
    with pytest.raises(FlatGeobufCorruptHeaderError):
        parse_fgb_header(b"")
    with pytest.raises(FlatGeobufCorruptHeaderError):
        parse_fgb_header(b"fgb\x03")
    with pytest.raises(FlatGeobufCorruptHeaderError):
        parse_fgb_header(b"fgb\x03fgb\x00\x10\x00\x00")  # 11 bytes

    # Valid magic and length, but truncated before header end
    magic = magicbytes
    length_prefix = struct.pack("<I", 256)  # Declares 256 bytes header
    partial_bytes = b"\x00" * 20  # Only 20 bytes provided
    with pytest.raises(FlatGeobufCorruptHeaderError) as exc_info:
        parse_fgb_header(magic + length_prefix + partial_bytes)
    assert "Truncated FlatGeobuf payload" in str(exc_info.value)


def test_invalid_header_length_bounds():
    """Verify that absurdly small or large header sizes raise FlatGeobufCorruptHeaderError."""
    magic = magicbytes
    # Header length < 8
    too_small_len = struct.pack("<I", 4)
    with pytest.raises(FlatGeobufCorruptHeaderError):
        parse_fgb_header(magic + too_small_len + b"\x00" * 4)

    # Header length > 10MB
    too_large_len = struct.pack("<I", 20 * 1024 * 1024)
    with pytest.raises(FlatGeobufCorruptHeaderError):
        parse_fgb_header(magic + too_large_len + b"\x00" * 16)


def test_spatial_index_flag_and_tree_calculation():
    """Verify spatial index presence and Hilbert R-tree index calculation."""
    # Case 1: Index node size > 0 and features > 0 -> index present
    p_with_index = build_synthetic_fgb_payload(features_count=100, index_node_size=16)
    h_with_index = parse_fgb_header(p_with_index)
    assert h_with_index.has_spatial_index is True
    assert h_with_index.index_node_size == 16
    expected_tree_size = calc_tree_size(100, 16)
    assert h_with_index.index_length == expected_tree_size
    assert h_with_index.index_length > 0

    # Case 2: Index node size == 0 -> no index
    p_no_index = build_synthetic_fgb_payload(features_count=100, index_node_size=0)
    h_no_index = parse_fgb_header(p_no_index)
    assert h_no_index.has_spatial_index is False
    assert h_no_index.index_length == 0

    # Case 3: Features count == 0 -> no index
    p_zero_feat = build_synthetic_fgb_payload(features_count=0, index_node_size=16)
    h_zero_feat = parse_fgb_header(p_zero_feat)
    assert h_zero_feat.has_spatial_index is False
    assert h_zero_feat.index_length == 0


def test_crs_identification_and_canonical_crs():
    """Verify CRS extraction for Everest 1830, UTM 43N, WGS84, and fallback."""
    # 1. Everest 1830 / Kalianpur (EPSG:24378)
    p_everest = build_synthetic_fgb_payload(crs_code=24378, crs_name="Kalianpur 1975 / India Zone IIa")
    h_everest = parse_fgb_header(p_everest)
    assert h_everest.canonical_crs == "EPSG:24378"

    # 2. WGS 84 / UTM zone 43N (EPSG:32643)
    p_utm = build_synthetic_fgb_payload(crs_code=32643, crs_name="WGS 84 / UTM zone 43N")
    h_utm = parse_fgb_header(p_utm)
    assert h_utm.canonical_crs == "EPSG:32643"

    # 3. WGS 84 geographic (EPSG:4326)
    p_wgs84 = build_synthetic_fgb_payload(crs_code=4326, crs_name="WGS 84")
    h_wgs84 = parse_fgb_header(p_wgs84)
    assert h_wgs84.canonical_crs == "EPSG:4326"

    # 4. CRS code is 0 but crs_name has EPSG substring
    p_name_only = build_synthetic_fgb_payload(crs_code=0, crs_name="Projected Everest 1830 EPSG:24378")
    h_name_only = parse_fgb_header(p_name_only)
    assert h_name_only.canonical_crs == "EPSG:24378"

    # 5. Empty CRS fallback to EPSG:4326
    p_no_crs = build_synthetic_fgb_payload(crs_code=0, crs_name="")
    h_no_crs = parse_fgb_header(p_no_crs)
    assert h_no_crs.canonical_crs == "EPSG:4326"


def test_envelope_extraction():
    """Verify envelope bounding box coordinates extraction."""
    # Given bbox
    bbox = (77.10, 28.50, 77.30, 28.70)
    payload = build_synthetic_fgb_payload(envelope=bbox)
    header = parse_fgb_header(payload)
    assert header.envelope == pytest.approx(bbox)

    # Empty bbox
    payload_none = build_synthetic_fgb_payload(envelope=None)
    header_none = parse_fgb_header(payload_none)
    assert header_none.envelope is None


def test_flatgeobuf_ingestion_reader_from_file_and_bytes(tmp_path: Path):
    """Verify FlatGeobufIngestionReader end-to-end file and byte reader interface."""
    fgb_path = tmp_path / "cadastral_parcels.fgb"
    payload = build_synthetic_fgb_payload(
        name="delhi_urban_parcels",
        features_count=42,
        crs_code=32643,
        envelope=(700000.0, 3100000.0, 705000.0, 3105000.0),
    )
    fgb_path.write_bytes(payload)

    # 1. Read header from file
    header_from_file = FlatGeobufIngestionReader.read_header_from_file(fgb_path)
    assert header_from_file.name == "delhi_urban_parcels"
    assert header_from_file.features_count == 42
    assert header_from_file.canonical_crs == "EPSG:32643"

    # 2. Read full result from file
    res_from_file = FlatGeobufIngestionReader.read_from_file(fgb_path)
    assert isinstance(res_from_file, FlatGeobufIngestionResult)
    assert res_from_file.detected_crs == "EPSG:32643"
    assert res_from_file.total_count == 42
    assert res_from_file.valid_count == 42
    assert res_from_file.has_spatial_index is True
    assert res_from_file.bounds == (700000.0, 3100000.0, 705000.0, 3105000.0)

    # 3. Read header from bytes
    header_from_bytes = FlatGeobufIngestionReader.read_header_from_bytes(payload)
    assert header_from_bytes.name == "delhi_urban_parcels"
    assert header_from_bytes.features_count == 42

    # 4. Read full result from bytes
    res_from_bytes = FlatGeobufIngestionReader.read_from_bytes(payload, target_crs="EPSG:32643")
    assert res_from_bytes.detected_crs == "EPSG:32643"
    assert res_from_bytes.total_count == 42


def test_flatgeobuf_reader_nonexistent_and_corrupt_files(tmp_path: Path):
    """Verify file error handling for missing and invalid files."""
    missing_file = tmp_path / "nonexistent.fgb"
    with pytest.raises(FileNotFoundError):
        FlatGeobufIngestionReader.read_header_from_file(missing_file)

    corrupt_file = tmp_path / "corrupt.fgb"
    corrupt_file.write_bytes(b"short")
    with pytest.raises(FlatGeobufCorruptHeaderError):
        FlatGeobufIngestionReader.read_header_from_file(corrupt_file)
