"""Tests for corrupt archive, path traversal, zip bombs, and malformed header rejection."""

import io
import json
from pathlib import Path
import struct
import zipfile

import pytest
from shapely.geometry import Polygon
import shapefile

from backend.ingestion.flatgeobuf_reader import build_synthetic_fgb_payload
from backend.ingestion.geotiff_reader import SyntheticGeoTIFFPage, build_synthetic_geotiff
from backend.ingestion.validation import (
    CorruptArchiveError,
    MalformedHeaderError,
    PathTraversalError,
    PayloadSizeExceededError,
    UnsupportedPayloadError,
    UploadValidationError,
    UploadValidationReport,
    UploadValidator,
    ZipBombError,
)
from backend.models.layer import SupportedFormat
from fixtures.defect_fixtures import generate_synthetic_dxf_cadastre


def _create_minimal_valid_shapefile_zip() -> bytes:
    """Helper generating minimal valid Shapefile ZIP."""
    shp_buf = io.BytesIO()
    shx_buf = io.BytesIO()
    dbf_buf = io.BytesIO()

    with shapefile.Writer(shp=shp_buf, shx=shx_buf, dbf=dbf_buf, shapeType=shapefile.POLYGON) as w:
        w.field("khasra_no", "C", size=50)
        w.poly([[[77.0, 28.0], [77.1, 28.0], [77.1, 28.1], [77.0, 28.1], [77.0, 28.0]]])
        w.record(khasra_no="101/1")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("cadastre.shp", shp_buf.getvalue())
        zf.writestr("cadastre.shx", shx_buf.getvalue())
        zf.writestr("cadastre.dbf", dbf_buf.getvalue())
        zf.writestr("cadastre.prj", 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]')
    return zip_buf.getvalue()


# ---------------------------------------------------------------------------
# Corrupt and Malicious Archive Rejection Tests
# ---------------------------------------------------------------------------


def test_archive_truncated_or_empty_bytes_rejected():
    """Verify empty or truncated byte sequences are rejected as CorruptArchiveError."""
    with pytest.raises(CorruptArchiveError, match="empty or truncated"):
        UploadValidator.validate_archive_security(b"")

    with pytest.raises(CorruptArchiveError, match="empty or truncated"):
        UploadValidator.validate_archive_security(b"PK")

    with pytest.raises(CorruptArchiveError, match="valid ZIP signature"):
        UploadValidator.validate_archive_security(b"THIS_IS_NOT_A_ZIP_HEADER_AT_ALL")


def test_archive_truncated_central_directory_rejected():
    """Verify ZIP files with corrupt headers or truncated central directories fail."""
    # Incomplete ZIP header
    corrupt_zip = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 20
    with pytest.raises(CorruptArchiveError, match="Malformed ZIP archive"):
        UploadValidator.validate_archive_security(corrupt_zip)


def test_archive_empty_zero_files_rejected():
    """Verify an archive containing zero entries is rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass  # 0 files

    with pytest.raises(CorruptArchiveError, match="contains 0 files"):
        UploadValidator.validate_archive_security(buf.getvalue())


def test_archive_path_traversal_relative_parent_rejected():
    """Verify archive entries containing '../' directory traversal are blocked."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/passwd", b"root:x:0:0:::")
        zf.writestr("cadastre.shp", b"dummy")

    with pytest.raises(PathTraversalError, match="Directory traversal detected"):
        UploadValidator.validate_archive_security(buf.getvalue())


def test_archive_path_traversal_windows_and_absolute_paths_rejected():
    """Verify absolute paths and drive letters in ZIP entries are rejected."""
    # Absolute Unix path
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/var/data/parcels.shp", b"dummy")

    with pytest.raises(PathTraversalError, match="Absolute or drive-qualified path"):
        UploadValidator.validate_archive_security(buf.getvalue())

    # Windows drive qualified path
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("C:/Windows/System32/evil.shp", b"dummy")

    with pytest.raises(PathTraversalError, match="Absolute or drive-qualified path"):
        UploadValidator.validate_archive_security(buf2.getvalue())


def test_archive_forbidden_null_byte_in_filename_rejected():
    """Verify filenames containing null bytes are blocked."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("parcels.shp", b"dummy")

    raw_with_null = buf.getvalue().replace(b"parcels.shp", b"par\x00els.shp")
    with pytest.raises(PathTraversalError, match="null byte"):
        UploadValidator.validate_archive_security(raw_with_null)


def test_archive_zip_bomb_excessive_entry_count_rejected():
    """Verify archives with excessive entry count exceeding ceiling are blocked."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(1005):
            zf.writestr(f"file_{i}.txt", b"x")

    with pytest.raises(ZipBombError, match="ZIP entry count.*exceeds maximum"):
        UploadValidator.validate_archive_security(buf.getvalue())


def test_archive_zip_bomb_excessive_decompressed_size_rejected():
    """Verify archives with aggregate uncompressed size over ceiling are rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("large.shp", b"x" * 100)

    # Patch central directory header uncompressed file size field to 600 MB (> 500 MB limit)
    raw = bytearray(buf.getvalue())
    cd_idx = raw.find(b"PK\x01\x02")
    assert cd_idx != -1
    raw[cd_idx + 24 : cd_idx + 28] = struct.pack("<I", 600 * 1024 * 1024)

    with pytest.raises(ZipBombError, match="Uncompressed archive footprint.*exceeds ceiling"):
        UploadValidator.validate_archive_security(bytes(raw))


def test_archive_crc_corruption_rejected():
    """Verify archives with corrupted member CRC checksums fail validation."""
    valid_zip = _create_minimal_valid_shapefile_zip()
    # Corrupt payload byte inside zip data (tampering with compressed file content)
    corrupt_data = bytearray(valid_zip)
    # Mutate byte in the middle of compressed file stream
    corrupt_data[150] = (corrupt_data[150] + 1) % 256

    with pytest.raises(CorruptArchiveError, match="Corrupted CRC checksum"):
        UploadValidator.validate_archive_security(bytes(corrupt_data))


# ---------------------------------------------------------------------------
# Shapefile Archive Header and Component Integrity Tests
# ---------------------------------------------------------------------------


def test_shapefile_missing_mandatory_components():
    """Verify Shapefile archive missing .shp, .shx, or .dbf is rejected."""
    # Missing .dbf
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cadastre.shp", struct.pack(">I", 9994) + b"\x00" * 96)
        zf.writestr("cadastre.shx", b"\x00" * 100)

    with pytest.raises(MalformedHeaderError, match="missing mandatory .dbf"):
        UploadValidator.validate_shapefile_archive(buf.getvalue())

    # Missing .shp
    buf_noshp = io.BytesIO()
    with zipfile.ZipFile(buf_noshp, "w") as zf:
        zf.writestr("cadastre.shx", b"\x00" * 100)
        zf.writestr("cadastre.dbf", b"\x00" * 100)

    with pytest.raises(MalformedHeaderError, match="missing mandatory .shp"):
        UploadValidator.validate_shapefile_archive(buf_noshp.getvalue())


def test_shapefile_truncated_header_inside_archive():
    """Verify Shapefile with .shp component shorter than 100 bytes is rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cadastre.shp", struct.pack(">I", 9994) + b"\x00" * 50)  # Only 54 bytes
        zf.writestr("cadastre.shx", b"\x00" * 100)
        zf.writestr("cadastre.dbf", b"\x00" * 100)

    with pytest.raises(MalformedHeaderError, match="header truncated.*minimum 100 required"):
        UploadValidator.validate_shapefile_archive(buf.getvalue())


def test_shapefile_invalid_file_code_in_archive():
    """Verify Shapefile with invalid file code (not 9994) is rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cadastre.shp", struct.pack(">I", 1234) + b"\x00" * 96)
        zf.writestr("cadastre.shx", b"\x00" * 100)
        zf.writestr("cadastre.dbf", b"\x00" * 100)

    with pytest.raises(MalformedHeaderError, match="Invalid Shapefile code 1234"):
        UploadValidator.validate_shapefile_archive(buf.getvalue())


def test_shapefile_inverted_bounding_box_in_header():
    """Verify Shapefile header with inverted bounding box (minx > maxx) is rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Construct 100-byte header with inverted bounds: minx=100.0 > maxx=50.0
        header = bytearray(struct.pack(">I", 9994) + b"\x00" * 96)
        header[36:68] = struct.pack("<4d", 100.0, 10.0, 50.0, 20.0)  # minx=100, miny=10, maxx=50, maxy=20
        zf.writestr("cadastre.shp", bytes(header))
        zf.writestr("cadastre.shx", b"\x00" * 100)
        zf.writestr("cadastre.dbf", b"\x00" * 32)

    with pytest.raises(MalformedHeaderError, match="inverted bounding box"):
        UploadValidator.validate_shapefile_archive(buf.getvalue())


# ---------------------------------------------------------------------------
# FlatGeobuf Header Integrity Tests
# ---------------------------------------------------------------------------


def test_flatgeobuf_truncated_payload_rejected():
    """Verify truncated FlatGeobuf payload under 12 bytes is rejected."""
    with pytest.raises(MalformedHeaderError, match="payload too short"):
        UploadValidator.validate_flatgeobuf_header(b"fgb\x03")


def test_flatgeobuf_invalid_magic_bytes_rejected():
    """Verify FlatGeobuf with corrupt magic bytes fails validation."""
    with pytest.raises(MalformedHeaderError, match="Invalid FlatGeobuf magic"):
        UploadValidator.validate_flatgeobuf_header(b"bad\x03fgb\x00\x00\x00\x00\x00")

    with pytest.raises(MalformedHeaderError, match="Invalid FlatGeobuf magic"):
        UploadValidator.validate_flatgeobuf_header(b"fgb\x02fgb\x00\x00\x00\x00\x00")


def test_flatgeobuf_absurd_header_length_rejected():
    """Verify FlatGeobuf declaring header length > 10MB is rejected."""
    corrupt_hdr = b"fgb\x03fgb\x00" + struct.pack("<I", 50 * 1024 * 1024)
    with pytest.raises(MalformedHeaderError, match="Absurd FlatGeobuf header length"):
        UploadValidator.validate_flatgeobuf_header(corrupt_hdr)


def test_flatgeobuf_truncated_declared_header_rejected():
    """Verify FlatGeobuf declaring more header bytes than file length is rejected."""
    corrupt_hdr = b"fgb\x03fgb\x00" + struct.pack("<I", 1024) + b"\x00" * 100
    with pytest.raises(MalformedHeaderError, match="Truncated FlatGeobuf payload"):
        UploadValidator.validate_flatgeobuf_header(corrupt_hdr)


# ---------------------------------------------------------------------------
# GeoTIFF Header Integrity Tests
# ---------------------------------------------------------------------------


def test_geotiff_truncated_header_rejected():
    """Verify GeoTIFF with under 8 bytes is rejected."""
    with pytest.raises(MalformedHeaderError, match="payload too short"):
        UploadValidator.validate_geotiff_header(b"II*\x00")


def test_geotiff_invalid_byte_order_rejected():
    """Verify GeoTIFF with invalid endianness indicator is rejected."""
    with pytest.raises(MalformedHeaderError, match="Invalid TIFF byte order"):
        UploadValidator.validate_geotiff_header(b"XX*\x00\x08\x00\x00\x00")


def test_geotiff_unsupported_version_rejected():
    """Verify GeoTIFF with unknown version number is rejected."""
    with pytest.raises(MalformedHeaderError, match="Unsupported TIFF version"):
        UploadValidator.validate_geotiff_header(b"II\x99\x00\x08\x00\x00\x00")


def test_geotiff_ifd_offset_outside_file_rejected():
    """Verify GeoTIFF with IFD offset pointing beyond payload size is rejected."""
    # IFD offset 99999 but payload only 16 bytes
    bad_offset = b"II*\x00" + struct.pack("<I", 99999) + b"\x00" * 8
    with pytest.raises(MalformedHeaderError, match="points outside payload bounds"):
        UploadValidator.validate_geotiff_header(bad_offset)


# ---------------------------------------------------------------------------
# GeoJSON Header and Payload Integrity Tests
# ---------------------------------------------------------------------------


def test_geojson_syntax_error_rejected():
    """Verify unclosed / malformed JSON syntax is rejected."""
    with pytest.raises(MalformedHeaderError, match="Malformed JSON syntax"):
        UploadValidator.validate_geojson_payload(b'{"type": "FeatureCollection", "features": [')


def test_geojson_non_utf8_encoding_rejected():
    """Verify non-UTF8 encoded byte stream is rejected."""
    with pytest.raises(MalformedHeaderError, match="must be valid UTF-8"):
        UploadValidator.validate_geojson_payload(b'{"type": "FeatureCollection", "name": "\xff\xfe"}')


def test_geojson_non_dict_root_rejected():
    """Verify GeoJSON payload with non-dict root (e.g., list, integer) is rejected."""
    with pytest.raises(MalformedHeaderError, match="root must be an object"):
        UploadValidator.validate_geojson_payload(b'[1, 2, 3]')

    with pytest.raises(MalformedHeaderError, match="root must be an object"):
        UploadValidator.validate_geojson_payload(b'"just a string"')


def test_geojson_missing_or_invalid_type_rejected():
    """Verify GeoJSON missing 'type' or with unsupported type is rejected."""
    with pytest.raises(MalformedHeaderError, match="missing mandatory string 'type'"):
        UploadValidator.validate_geojson_payload(b'{"features": []}')

    with pytest.raises(MalformedHeaderError, match="Unsupported GeoJSON type"):
        UploadValidator.validate_geojson_payload(b'{"type": "UnsupportedGeometryCollection"}')


def test_geojson_feature_collection_missing_features_array():
    """Verify FeatureCollection missing 'features' list is rejected."""
    with pytest.raises(MalformedHeaderError, match="missing 'features' array"):
        UploadValidator.validate_geojson_payload(b'{"type": "FeatureCollection"}')


# ---------------------------------------------------------------------------
# DXF Header Integrity Tests
# ---------------------------------------------------------------------------


def test_dxf_empty_payload_rejected():
    """Verify 0-byte DXF is rejected."""
    with pytest.raises(MalformedHeaderError, match="payload is empty"):
        UploadValidator.validate_dxf_header(b"")


def test_dxf_truncated_binary_header_rejected():
    """Verify binary DXF under 24 bytes is rejected."""
    with pytest.raises(MalformedHeaderError, match="Binary DXF header truncated"):
        UploadValidator.validate_dxf_header(b"AutoCAD Binary DXF\r\n")


def test_dxf_invalid_ascii_structure_rejected():
    """Verify ASCII DXF lacking group code structure is rejected."""
    with pytest.raises(MalformedHeaderError, match="does not begin with valid group-code"):
        UploadValidator.validate_dxf_header(b"RANDOM TEXT DATA NOT DXF CAD DRAWING")


# ---------------------------------------------------------------------------
# UploadValidator.validate End-to-End Suite Tests
# ---------------------------------------------------------------------------


def test_validator_valid_uploads_across_all_formats():
    """Verify UploadValidator.validate accepts valid datasets across formats."""
    # 1. GeoJSON
    gj_data = json.dumps({"type": "FeatureCollection", "features": []}).encode("utf-8")
    rep_gj = UploadValidator.validate(gj_data, filename="parcels.geojson")
    assert rep_gj.is_valid is True
    assert rep_gj.format == SupportedFormat.GEOJSON
    assert rep_gj.mime_type == "application/geo+json"
    assert rep_gj.error_code is None

    # 2. Shapefile ZIP
    shp_data = _create_minimal_valid_shapefile_zip()
    rep_shp = UploadValidator.validate(shp_data, filename="survey.zip")
    assert rep_shp.is_valid is True
    assert rep_shp.format == SupportedFormat.SHAPEFILE
    assert rep_shp.details.get("has_prj") is True

    # 3. FlatGeobuf
    fgb_data = build_synthetic_fgb_payload(name="kalianpur", features_count=10)
    rep_fgb = UploadValidator.validate(fgb_data, filename="parcels.fgb")
    assert rep_fgb.is_valid is True
    assert rep_fgb.format == SupportedFormat.FLATGEOBUF
    assert rep_fgb.mime_type == "application/vnd.flatgeobuf"

    # 4. GeoTIFF
    spec = SyntheticGeoTIFFPage(width=10, height=10, tiepoints=[(0.0, 0.0, 0.0, 100.0, 100.0, 0.0)], pixel_scale=(1.0, 1.0, 0.0), epsg=24378)
    gt_data = build_synthetic_geotiff([spec])
    rep_gt = UploadValidator.validate(gt_data, filename="orthophoto.tif")
    assert rep_gt.is_valid is True
    assert rep_gt.format == SupportedFormat.GEOTIFF
    assert rep_gt.mime_type == "image/tiff"

    # 5. DXF
    dxf_data = generate_synthetic_dxf_cadastre().encode("utf-8")
    rep_dxf = UploadValidator.validate(dxf_data, filename="cadastre.dxf")
    assert rep_dxf.is_valid is True
    assert rep_dxf.format == SupportedFormat.DXF


def test_validator_empty_payload_report():
    """Verify empty upload produces structured error report."""
    rep = UploadValidator.validate(b"", filename="empty.geojson")
    assert rep.is_valid is False
    assert rep.error_code == "EMPTY_PAYLOAD"
    assert "0 bytes" in rep.error_message


def test_validator_payload_size_exceeded_report():
    """Verify upload exceeding maximum byte size is rejected with PAYLOAD_SIZE_EXCEEDED."""
    data = b"x" * 1024
    rep = UploadValidator.validate(data, filename="big.geojson", max_size_bytes=512)
    assert rep.is_valid is False
    assert rep.error_code == "PAYLOAD_SIZE_EXCEEDED"
    assert rep.file_size_bytes == 1024


def test_validator_path_traversal_report():
    """Verify path traversal in ZIP produces structured PATH_TRAVERSAL report."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/shadow", b"secret")
    rep = UploadValidator.validate(buf.getvalue(), filename="traversal.zip")
    assert rep.is_valid is False
    assert rep.error_code == "PATH_TRAVERSAL"
    assert "Directory traversal" in rep.error_message


def test_validator_zip_bomb_report():
    """Verify zip bomb in upload produces structured ZIP_BOMB report."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(1005):
            zf.writestr(f"f_{i}.txt", b"x")
    rep = UploadValidator.validate(buf.getvalue(), filename="bomb.zip")
    assert rep.is_valid is False
    assert rep.error_code == "ZIP_BOMB"


def test_validator_malformed_header_report():
    """Verify corrupt FlatGeobuf header produces structured MALFORMED_HEADER report."""
    bad_fgb = b"bad\x03fgb\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    rep = UploadValidator.validate(bad_fgb, filename="bad.fgb", strict_magic=False)
    assert rep.is_valid is False
    assert rep.error_code == "MALFORMED_HEADER"


def test_upload_validation_report_to_dict():
    """Verify UploadValidationReport serialization to JSON-friendly dictionary."""
    rep = UploadValidationReport(
        is_valid=True,
        format=SupportedFormat.GEOJSON,
        mime_type="application/geo+json",
        file_size_bytes=42,
        details={"features_count": 5},
    )
    d = rep.to_dict()
    assert d["is_valid"] is True
    assert d["format"] == "GEOJSON"
    assert d["mime_type"] == "application/geo+json"
    assert d["file_size_bytes"] == 42
    assert d["details"]["features_count"] == 5


# ---------------------------------------------------------------------------
# MIME Type, Magic Byte and Extension Verification Tests (Step 48)
# ---------------------------------------------------------------------------


def _valid_geojson_bytes() -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": []}).encode("utf-8")


def test_normalize_mime_type_strips_parameters_and_case():
    assert UploadValidator.normalize_mime_type("Application/GEO+JSON; charset=UTF-8") == "application/geo+json"
    assert UploadValidator.normalize_mime_type("  ") is None
    assert UploadValidator.normalize_mime_type(None) is None


def test_declared_mime_matching_detected_format_accepted():
    rep = UploadValidator.validate(
        _valid_geojson_bytes(), filename="parcels.geojson", declared_mime_type="application/geo+json; charset=utf-8"
    )
    assert rep.is_valid is True
    assert rep.details["declared_mime_type"] == "application/geo+json"
    assert rep.details["magic_verified"] is True


def test_generic_octet_stream_mime_tolerated():
    rep = UploadValidator.validate(
        _valid_geojson_bytes(), filename="parcels.geojson", declared_mime_type="application/octet-stream"
    )
    assert rep.is_valid is True


def test_declared_mime_conflicting_with_detected_format_rejected():
    # GeoJSON bytes uploaded while claiming to be a TIFF image
    rep = UploadValidator.validate(
        _valid_geojson_bytes(), filename="parcels.geojson", declared_mime_type="image/tiff"
    )
    assert rep.is_valid is False
    assert rep.error_code == "MIME_TYPE_MISMATCH"
    assert rep.format == SupportedFormat.GEOJSON


def test_unknown_declared_mime_rejected():
    rep = UploadValidator.validate(
        _valid_geojson_bytes(), filename="parcels.geojson", declared_mime_type="video/mp4"
    )
    assert rep.is_valid is False
    assert rep.error_code == "UNSUPPORTED_MIME_TYPE"


def test_shapefile_zip_accepts_zip_mime_types():
    data = _create_minimal_valid_shapefile_zip()
    for mime in ("application/zip", "application/x-zip-compressed"):
        rep = UploadValidator.validate(data, filename="survey.zip", declared_mime_type=mime)
        assert rep.is_valid is True, mime


def test_extension_mismatch_with_magic_bytes_rejected():
    # FlatGeobuf bytes disguised with a .tif extension
    fgb = build_synthetic_fgb_payload(name="kalianpur", features_count=3)
    rep = UploadValidator.validate(fgb, filename="orthophoto.tif")
    assert rep.is_valid is False
    assert rep.error_code == "EXTENSION_MISMATCH"
    assert rep.format == SupportedFormat.FLATGEOBUF


def test_unknown_extension_with_valid_magic_is_accepted():
    rep = UploadValidator.validate(_valid_geojson_bytes(), filename="upload.dat")
    assert rep.is_valid is True


def test_filename_only_detection_rejected_when_strict_magic():
    # Garbage bytes labelled .fgb are not a FlatGeobuf file
    rep = UploadValidator.validate(b"bad\x03fgb\x00" + b"\x00" * 16, filename="fake.fgb")
    assert rep.is_valid is False
    assert rep.error_code == "MAGIC_BYTE_MISMATCH"


def test_filename_only_detection_allowed_when_strict_magic_disabled():
    rep = UploadValidator.validate(b"bad\x03fgb\x00" + b"\x00" * 16, filename="fake.fgb", strict_magic=False)
    assert rep.is_valid is False
    assert rep.error_code == "MALFORMED_HEADER"


@pytest.mark.parametrize(
    "payload,label",
    [
        (b"MZ\x90\x00" + b"\x00" * 64, "Windows PE"),
        (b"\x7fELF\x02\x01\x01" + b"\x00" * 64, "ELF"),
        (b"#!/bin/sh\nrm -rf /\n", "shebang"),
    ],
)
def test_executable_payloads_rejected_even_with_geo_extension(payload, label):
    rep = UploadValidator.validate(payload, filename="parcels.geojson", declared_mime_type="application/geo+json")
    assert rep.is_valid is False
    assert rep.error_code == "EXECUTABLE_PAYLOAD"
    assert label.split()[0].lower() in rep.error_message.lower() or "executable" in rep.error_message.lower()


def test_verify_mime_and_extension_returns_none_for_consistent_upload():
    from backend.ingestion.pipeline import GeospatialFormatDetector

    det = GeospatialFormatDetector.detect_bytes(_valid_geojson_bytes(), filename="a.geojson")
    assert UploadValidator.verify_mime_and_extension(det, "a.geojson", "application/json") is None


def test_allowed_mime_and_extension_tables_cover_all_formats():
    for fmt in (
        SupportedFormat.GEOJSON,
        SupportedFormat.SHAPEFILE,
        SupportedFormat.DXF,
        SupportedFormat.FLATGEOBUF,
        SupportedFormat.GEOTIFF,
        SupportedFormat.KML,
        SupportedFormat.KMZ,
    ):
        assert UploadValidator.ALLOWED_MIME_TYPES[fmt]
        assert UploadValidator.ALLOWED_EXTENSIONS[fmt]
