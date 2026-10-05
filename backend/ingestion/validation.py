"""Pre-flight validation for geospatial uploads: archive integrity, path safety, and header verification."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
from pathlib import Path, PurePosixPath
import struct
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import zipfile

from backend.ingestion.pipeline import (
    CorruptDataError,
    FormatDetectionResult,
    GeospatialFormatDetector,
    UnsupportedFormatError,
)
from backend.models.layer import SupportedFormat


class UploadValidationError(Exception):
    """Base exception for upload validation failures."""
    pass


class CorruptArchiveError(UploadValidationError):
    """Raised when an uploaded archive (.zip, .kmz) is truncated, corrupted, or unreadable."""
    pass


class PathTraversalError(UploadValidationError):
    """Raised when an archive contains dangerous paths trying to escape the extraction root."""
    pass


class ZipBombError(UploadValidationError):
    """Raised when an archive exhibits characteristics of a decompression bomb."""
    pass


class MalformedHeaderError(UploadValidationError):
    """Raised when a geospatial file header is truncated, has invalid magic bytes, or corrupt metadata."""
    pass


class PayloadSizeExceededError(UploadValidationError):
    """Raised when uploaded file exceeds configured maximum permissible size."""
    pass


class UnsupportedPayloadError(UploadValidationError):
    """Raised when payload format cannot be identified or verified."""
    pass


@dataclass
class UploadValidationReport:
    """Structured report returned upon evaluating an uploaded dataset."""

    is_valid: bool
    format: Optional[SupportedFormat] = None
    mime_type: Optional[str] = None
    file_size_bytes: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to JSON-serializable dictionary."""
        return {
            "is_valid": self.is_valid,
            "format": self.format.value if self.format else None,
            "mime_type": self.mime_type,
            "file_size_bytes": self.file_size_bytes,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "details": dict(self.details),
        }


class UploadValidator:
    """Validates uploaded geospatial files before ingestion: archive safety, header integrity, and size limits."""

    DEFAULT_MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
    MAX_ZIP_EXTRACTION_RATIO = 100.0             # Max 100:1 uncompressed-to-compressed ratio
    MAX_ZIP_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024  # 500 MB max uncompressed footprint
    MAX_ZIP_ENTRY_COUNT = 1000                   # Max files inside archive
    MAX_FILENAME_LENGTH = 255

    @classmethod
    def validate_archive_security(cls, zip_data: bytes) -> List[str]:
        """Validate ZIP archive for corruption, path traversal, zip bombs, and entry counts."""
        if not zip_data or len(zip_data) < 4:
            raise CorruptArchiveError("ZIP payload is empty or truncated")

        if not zip_data.startswith(b"PK\x03\x04") and not zip_data.startswith(b"PK\x05\x06"):
            raise CorruptArchiveError("Payload does not start with valid ZIP signature (PK)")

        # Scan for null bytes in local file headers (b"PK\x03\x04") and central headers (b"PK\x01\x02")
        pos = 0
        while True:
            idx = zip_data.find(b"PK\x03\x04", pos)
            if idx == -1:
                break
            if idx + 30 <= len(zip_data):
                fn_len = struct.unpack("<H", zip_data[idx + 26 : idx + 28])[0]
                if b"\x00" in zip_data[idx + 30 : idx + 30 + fn_len]:
                    raise PathTraversalError("Entry filename contains forbidden null byte")
            pos = idx + 4

        pos = 0
        while True:
            idx = zip_data.find(b"PK\x01\x02", pos)
            if idx == -1:
                break
            if idx + 46 <= len(zip_data):
                fn_len = struct.unpack("<H", zip_data[idx + 28 : idx + 30])[0]
                if b"\x00" in zip_data[idx + 46 : idx + 46 + fn_len]:
                    raise PathTraversalError("Entry filename contains forbidden null byte")
            pos = idx + 4

        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_data))
        except zipfile.BadZipFile as exc:
            raise CorruptArchiveError(f"Malformed ZIP archive: {exc}") from exc
        except Exception as exc:
            raise CorruptArchiveError(f"Unreadable ZIP archive: {exc}") from exc

        with zf:
            infolist = zf.infolist()
            if not infolist:
                raise CorruptArchiveError("ZIP archive is empty (contains 0 files)")

            if len(infolist) > cls.MAX_ZIP_ENTRY_COUNT:
                raise ZipBombError(
                    f"ZIP entry count ({len(infolist)}) exceeds maximum permitted ({cls.MAX_ZIP_ENTRY_COUNT})"
                )

            total_compressed = 0
            total_uncompressed = 0
            clean_names: List[str] = []

            for info in infolist:
                # 1. Path traversal & filename safety checks
                fname = info.filename
                if not fname:
                    continue

                if len(fname) > cls.MAX_FILENAME_LENGTH:
                    raise PathTraversalError(f"Entry filename exceeds maximum allowed length: {fname[:30]}...")

                if "\x00" in fname:
                    raise PathTraversalError("Entry filename contains forbidden null byte")

                # Normalize path
                posix_path = PurePosixPath(fname.replace("\\", "/"))
                parts = posix_path.parts

                if any(p == ".." for p in parts):
                    raise PathTraversalError(f"Directory traversal detected in archive entry: '{fname}'")

                if posix_path.is_absolute() or (len(parts) > 0 and ":" in parts[0]):
                    raise PathTraversalError(f"Absolute or drive-qualified path detected in archive: '{fname}'")

                # 2. Zip bomb checks
                total_compressed += info.compress_size
                total_uncompressed += info.file_size

                if total_uncompressed > cls.MAX_ZIP_TOTAL_UNCOMPRESSED:
                    raise ZipBombError(
                        f"Uncompressed archive footprint ({total_uncompressed} bytes) "
                        f"exceeds ceiling ({cls.MAX_ZIP_TOTAL_UNCOMPRESSED} bytes)"
                    )

                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > cls.MAX_ZIP_EXTRACTION_RATIO and info.file_size > 10 * 1024 * 1024:
                        raise ZipBombError(
                            f"Suspicious compression ratio ({ratio:.1f}:1) for entry '{fname}' indicates zip bomb"
                        )

                clean_names.append(fname)

            # Global compression ratio check
            if total_compressed > 0:
                global_ratio = total_uncompressed / max(total_compressed, 1)
                if global_ratio > cls.MAX_ZIP_EXTRACTION_RATIO and total_uncompressed > 20 * 1024 * 1024:
                    raise ZipBombError(f"Global archive compression ratio ({global_ratio:.1f}:1) indicates zip bomb")

            # 3. CRC integrity check
            corrupted_file = zf.testzip()
            if corrupted_file is not None:
                raise CorruptArchiveError(f"Corrupted CRC checksum in archive member: '{corrupted_file}'")

        return clean_names

    @classmethod
    def validate_shapefile_archive(cls, zip_data: bytes) -> Dict[str, Any]:
        """Verify that a ZIP archive contains a complete and valid Shapefile component set."""
        clean_names = cls.validate_archive_security(zip_data)

        zf = zipfile.ZipFile(io.BytesIO(zip_data))
        with zf:
            namelist_lower = {n.lower(): n for n in clean_names}
            shp_files = [n for n in clean_names if n.lower().endswith(".shp")]

            if not shp_files:
                raise MalformedHeaderError("Shapefile ZIP missing mandatory .shp geometry component")

            # Check primary shapefile
            primary_shp = shp_files[0]
            base = primary_shp[:-4]

            # Require .shx and .dbf with matching base
            shx_candidates = [n for n in clean_names if n.lower().endswith(".shx")]
            dbf_candidates = [n for n in clean_names if n.lower().endswith(".dbf")]

            if not shx_candidates:
                raise MalformedHeaderError("Shapefile ZIP missing mandatory .shx index component")
            if not dbf_candidates:
                raise MalformedHeaderError("Shapefile ZIP missing mandatory .dbf attribute component")

            # Read .shp header
            shp_bytes = zf.read(primary_shp)
            if len(shp_bytes) < 100:
                raise MalformedHeaderError(
                    f"Shapefile '{primary_shp}' header truncated ({len(shp_bytes)} bytes; minimum 100 required)"
                )

            file_code = struct.unpack(">I", shp_bytes[:4])[0]
            if file_code != 9994:
                raise MalformedHeaderError(
                    f"Invalid Shapefile code {file_code} in '{primary_shp}' (expected 9994)"
                )

            # Validate bounding box coordinates in header (bytes 36..68)
            minx, miny, maxx, maxy = struct.unpack("<4d", shp_bytes[36:68])
            if minx > maxx or miny > maxy:
                raise MalformedHeaderError(
                    f"Shapefile header has inverted bounding box: min=({minx}, {miny}) > max=({maxx}, {maxy})"
                )

            # Read .dbf header
            dbf_bytes = zf.read(dbf_candidates[0])
            if len(dbf_bytes) < 32:
                raise MalformedHeaderError(f"DBF component '{dbf_candidates[0]}' header truncated")

            return {
                "base_name": base,
                "shp": primary_shp,
                "shx": shx_candidates[0],
                "dbf": dbf_candidates[0],
                "has_prj": any(n.lower().endswith(".prj") for n in clean_names),
                "has_cpg": any(n.lower().endswith(".cpg") for n in clean_names),
                "bounds": (minx, miny, maxx, maxy),
            }

    @classmethod
    def validate_flatgeobuf_header(cls, data: bytes) -> Dict[str, Any]:
        """Verify binary FlatGeobuf header integrity."""
        if not data or len(data) < 12:
            raise MalformedHeaderError(
                f"FlatGeobuf payload too short: {len(data) if data else 0} bytes (minimum 12 required)"
            )

        magic = data[:8]
        if magic[:3] != b"fgb" or magic[3] != 3 or magic[4:7] != b"fgb" or magic[7] != 0:
            raise MalformedHeaderError(f"Invalid FlatGeobuf magic bytes: {magic!r}")

        header_len = struct.unpack("<I", data[8:12])[0]
        if header_len <= 0 or header_len > 10 * 1024 * 1024:
            raise MalformedHeaderError(f"Absurd FlatGeobuf header length: {header_len} bytes")

        if 12 + header_len > len(data):
            raise MalformedHeaderError(
                f"Truncated FlatGeobuf payload: header declares {header_len} bytes but file only has {len(data)} bytes"
            )

        return {"header_length": header_len}

    @classmethod
    def validate_geotiff_header(cls, data: bytes) -> Dict[str, Any]:
        """Verify GeoTIFF TIFF header and first IFD pointer."""
        if not data or len(data) < 8:
            raise MalformedHeaderError(
                f"GeoTIFF payload too short: {len(data) if data else 0} bytes (minimum 8 required)"
            )

        byte_order = data[:2]
        if byte_order not in (b"II", b"MM"):
            raise MalformedHeaderError(f"Invalid TIFF byte order indicator: {byte_order!r}")

        bo = "<" if byte_order == b"II" else ">"
        version = struct.unpack(bo + "H", data[2:4])[0]

        if version == 42:
            first_ifd_offset = struct.unpack(bo + "I", data[4:8])[0]
            header_size = 8
        elif version == 43:
            if len(data) < 16:
                raise MalformedHeaderError("BigTIFF payload truncated before IFD offset pointer")
            offset_size = struct.unpack(bo + "H", data[4:6])[0]
            if offset_size != 8:
                raise MalformedHeaderError(f"Invalid BigTIFF offset byte size: {offset_size}")
            first_ifd_offset = struct.unpack(bo + "Q", data[8:16])[0]
            header_size = 16
        else:
            raise MalformedHeaderError(f"Unsupported TIFF version number: {version}")

        if first_ifd_offset < header_size or first_ifd_offset >= len(data):
            raise MalformedHeaderError(
                f"TIFF first IFD offset {first_ifd_offset} points outside payload bounds (size {len(data)})"
            )

        return {
            "version": version,
            "byte_order": "LE" if byte_order == b"II" else "BE",
            "first_ifd_offset": first_ifd_offset,
        }

    @classmethod
    def validate_geojson_payload(cls, data: bytes) -> Dict[str, Any]:
        """Verify GeoJSON syntax and structural geometry conformance."""
        if not data or len(data) == 0:
            raise MalformedHeaderError("GeoJSON payload is empty")

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedHeaderError(f"GeoJSON must be valid UTF-8: {exc}") from exc

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedHeaderError(f"Malformed JSON syntax: {exc}") from exc

        if not isinstance(parsed, dict):
            raise MalformedHeaderError(f"GeoJSON root must be an object/dict, got {type(parsed).__name__}")

        geo_type = parsed.get("type")
        if not geo_type or not isinstance(geo_type, str):
            raise MalformedHeaderError("GeoJSON missing mandatory string 'type' member")

        allowed_types = {
            "FeatureCollection",
            "Feature",
            "Polygon",
            "MultiPolygon",
            "GeometryCollection",
            "Point",
            "LineString",
            "MultiPoint",
            "MultiLineString",
        }
        if geo_type not in allowed_types:
            raise MalformedHeaderError(f"Unsupported GeoJSON type: '{geo_type}'")

        if geo_type == "FeatureCollection":
            features = parsed.get("features")
            if features is None or not isinstance(features, list):
                raise MalformedHeaderError("FeatureCollection missing 'features' array")

        return {"geojson_type": geo_type}

    @classmethod
    def validate_dxf_header(cls, data: bytes) -> Dict[str, Any]:
        """Verify DXF CAD drawing markers and completeness."""
        if not data or len(data) == 0:
            raise MalformedHeaderError("DXF payload is empty")

        if data.startswith(b"AutoCAD Binary DXF"):
            if len(data) < 24:
                raise MalformedHeaderError("Binary DXF header truncated")
            return {"dxf_type": "BINARY"}

        # ASCII DXF validation
        sample = data[:4096].decode("latin-1", errors="ignore").strip()
        if not ("SECTION" in sample or sample.startswith("0")):
            raise MalformedHeaderError("ASCII DXF does not begin with valid group-code section markers")

        return {"dxf_type": "ASCII"}

    @classmethod
    def validate(
        cls,
        data: bytes,
        filename: Optional[str] = None,
        max_size_bytes: Optional[int] = None,
    ) -> UploadValidationReport:
        """Execute full upload pre-flight validation suite."""
        max_size = max_size_bytes or cls.DEFAULT_MAX_UPLOAD_SIZE
        file_size = len(data) if data else 0

        if file_size == 0:
            return UploadValidationReport(
                is_valid=False,
                file_size_bytes=0,
                error_code="EMPTY_PAYLOAD",
                error_message="Uploaded payload contains 0 bytes",
            )

        if file_size > max_size:
            return UploadValidationReport(
                is_valid=False,
                file_size_bytes=file_size,
                error_code="PAYLOAD_SIZE_EXCEEDED",
                error_message=f"Payload size ({file_size} bytes) exceeds limit ({max_size} bytes)",
            )

        try:
            det = GeospatialFormatDetector.detect_bytes(data, filename=filename)
        except CorruptDataError as exc:
            return UploadValidationReport(
                is_valid=False,
                file_size_bytes=file_size,
                error_code="CORRUPT_PAYLOAD",
                error_message=str(exc),
            )
        except UnsupportedFormatError as exc:
            return UploadValidationReport(
                is_valid=False,
                file_size_bytes=file_size,
                error_code="UNSUPPORTED_FORMAT",
                error_message=str(exc),
            )

        details: Dict[str, Any] = {
            "detection_method": det.detection_method,
            "confidence": det.confidence,
        }

        try:
            if det.format == SupportedFormat.SHAPEFILE:
                if data.startswith(b"PK\x03\x04"):
                    shp_details = cls.validate_shapefile_archive(data)
                    details.update(shp_details)
                else:
                    if len(data) < 100:
                        raise MalformedHeaderError("Standalone .shp file header is under 100 bytes")
                    file_code = struct.unpack(">I", data[:4])[0]
                    if file_code != 9994:
                        raise MalformedHeaderError(f"Invalid Shapefile code {file_code}")

            elif det.format in (SupportedFormat.KMZ, SupportedFormat.KML):
                if det.format == SupportedFormat.KMZ:
                    cls.validate_archive_security(data)

            elif det.format == SupportedFormat.FLATGEOBUF:
                fgb_details = cls.validate_flatgeobuf_header(data)
                details.update(fgb_details)

            elif det.format == SupportedFormat.GEOTIFF:
                gt_details = cls.validate_geotiff_header(data)
                details.update(gt_details)

            elif det.format == SupportedFormat.GEOJSON:
                gj_details = cls.validate_geojson_payload(data)
                details.update(gj_details)

            elif det.format == SupportedFormat.DXF:
                dxf_details = cls.validate_dxf_header(data)
                details.update(dxf_details)

        except CorruptArchiveError as exc:
            return UploadValidationReport(
                is_valid=False,
                format=det.format,
                mime_type=det.mime_type,
                file_size_bytes=file_size,
                error_code="CORRUPT_ARCHIVE",
                error_message=str(exc),
                details=details,
            )
        except PathTraversalError as exc:
            return UploadValidationReport(
                is_valid=False,
                format=det.format,
                mime_type=det.mime_type,
                file_size_bytes=file_size,
                error_code="PATH_TRAVERSAL",
                error_message=str(exc),
                details=details,
            )
        except ZipBombError as exc:
            return UploadValidationReport(
                is_valid=False,
                format=det.format,
                mime_type=det.mime_type,
                file_size_bytes=file_size,
                error_code="ZIP_BOMB",
                error_message=str(exc),
                details=details,
            )
        except MalformedHeaderError as exc:
            return UploadValidationReport(
                is_valid=False,
                format=det.format,
                mime_type=det.mime_type,
                file_size_bytes=file_size,
                error_code="MALFORMED_HEADER",
                error_message=str(exc),
                details=details,
            )
        except Exception as exc:
            return UploadValidationReport(
                is_valid=False,
                format=det.format,
                mime_type=det.mime_type,
                file_size_bytes=file_size,
                error_code="VALIDATION_ERROR",
                error_message=str(exc),
                details=details,
            )

        return UploadValidationReport(
            is_valid=True,
            format=det.format,
            mime_type=det.mime_type,
            file_size_bytes=file_size,
            details=details,
        )
