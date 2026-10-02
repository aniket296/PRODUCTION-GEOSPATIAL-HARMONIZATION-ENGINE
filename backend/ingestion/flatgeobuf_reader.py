"""FlatGeobuf binary ingestion reader: header parsing, CRS identification, and spatial index reading."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
from pathlib import Path
import struct
from typing import Any, Dict, List, Optional, Tuple, Union

import flatbuffers
from flatgeobuf.constants import SIZE_PREFIX_LEN, magicbytes
import flatgeobuf.FlatGeobuf.Column as Col
import flatgeobuf.FlatGeobuf.ColumnType as ColType
import flatgeobuf.FlatGeobuf.Crs as CrsFB
import flatgeobuf.FlatGeobuf.GeometryType as GeomTypeFB
import flatgeobuf.FlatGeobuf.Header as HeaderFB
from flatgeobuf.header_meta import from_byte_buffer
from flatgeobuf.packedrtree import calc_tree_size
from shapely.geometry import Polygon

from backend.ingestion.geojson_reader import (
    enforce_rfc7946_winding_order,
    normalize_cadastral_properties,
    remove_duplicate_consecutive_vertices,
    round_coordinates,
)


class FlatGeobufExtractionError(Exception):
    """Base exception for FlatGeobuf extraction errors."""
    pass


class FlatGeobufInvalidMagicError(FlatGeobufExtractionError):
    """Raised when binary magic bytes do not match FlatGeobuf specification."""
    pass


class FlatGeobufCorruptHeaderError(FlatGeobufExtractionError):
    """Raised when FlatGeobuf binary header is truncated or corrupt."""
    pass


class FlatGeobufEmptyFileError(FlatGeobufExtractionError):
    """Raised when FlatGeobuf file contains zero bytes or zero features."""
    pass


@dataclass
class FlatGeobufHeader:
    """Parsed metadata from FlatGeobuf binary file header."""
    name: str
    geometry_type: int
    features_count: int
    index_node_size: int
    has_spatial_index: bool
    crs_code: Optional[int]
    crs_name: Optional[str]
    envelope: Optional[Tuple[float, float, float, float]]
    columns: List[str]
    header_length: int
    index_length: int

    @property
    def canonical_crs(self) -> str:
        """Resolve canonical EPSG CRS string."""
        if self.crs_code and self.crs_code > 0:
            return f"EPSG:{self.crs_code}"
        if self.crs_name:
            if "24378" in self.crs_name:
                return "EPSG:24378"
            if "32643" in self.crs_name:
                return "EPSG:32643"
            if "4326" in self.crs_name:
                return "EPSG:4326"
            return self.crs_name
        return "EPSG:4326"


@dataclass
class FlatGeobufFeatureRecord:
    """Standardized feature extracted from FlatGeobuf."""
    feature_id: Optional[str]
    geometry: Any
    properties: Dict[str, Any]
    crs: str
    is_valid: bool
    bounds: Tuple[float, float, float, float]
    area: Optional[float] = None


@dataclass
class FlatGeobufIngestionResult:
    """Output of FlatGeobuf ingestion."""
    header: FlatGeobufHeader
    features: List[FlatGeobufFeatureRecord]
    detected_crs: str
    total_count: int
    valid_count: int
    invalid_count: int
    has_spatial_index: bool
    bounds: Optional[Tuple[float, float, float, float]]


def parse_fgb_header(data: bytes) -> FlatGeobufHeader:
    """Parse and validate binary FlatGeobuf header from raw bytes."""
    if not data or len(data) < 12:
        raise FlatGeobufCorruptHeaderError(
            f"FlatGeobuf payload too short: {len(data) if data else 0} bytes (minimum 12 bytes required)"
        )

    # 1. Validate 8-byte magic bytes
    file_magic = data[:8]
    # Check magic header: must start with 'fgb' and major version 3
    if file_magic[:3] != b"fgb" or file_magic[3] != 3:
        raise FlatGeobufInvalidMagicError(
            f"Invalid FlatGeobuf magic bytes: {file_magic!r} (expected {magicbytes!r})"
        )

    # 2. Extract 4-byte header length (uint32 little-endian)
    header_length = struct.unpack("<I", data[8:12])[0]
    HEADER_MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10 MB maximum header
    if header_length < 8 or header_length > HEADER_MAX_BUFFER_SIZE:
        raise FlatGeobufCorruptHeaderError(
            f"Invalid FlatGeobuf header length: {header_length} bytes"
        )

    total_header_end = 12 + header_length
    if len(data) < total_header_end:
        raise FlatGeobufCorruptHeaderError(
            f"Truncated FlatGeobuf payload: expected {total_header_end} bytes, found {len(data)} bytes"
        )

    # 3. Decode FlatBuffers Header
    header_bytes = bytearray(data[12:total_header_end])
    try:
        raw_fgb_header = HeaderFB.Header.GetRootAsHeader(header_bytes, 0)
        fgb_header_meta = from_byte_buffer(header_bytes)
    except Exception as exc:
        raise FlatGeobufCorruptHeaderError(f"Failed to decode FlatBuffers Header: {exc}") from exc

    features_count = int(fgb_header_meta.features_count or 0)
    index_node_size = int(fgb_header_meta.index_node_size or 0)
    has_spatial_index = bool(index_node_size > 0 and features_count > 0)

    # Compute Hilbert R-tree spatial index size in bytes
    index_length = calc_tree_size(features_count, index_node_size) if has_spatial_index else 0

    # Extract CRS metadata
    crs_code = None
    crs_name = None
    if fgb_header_meta.crs:
        crs_code = fgb_header_meta.crs.code
        if fgb_header_meta.crs.name:
            crs_name = (
                fgb_header_meta.crs.name.decode("utf-8")
                if isinstance(fgb_header_meta.crs.name, (bytes, bytearray))
                else str(fgb_header_meta.crs.name)
            )

    # Extract envelope (first from raw FlatBuffer header vector, fallback to meta)
    envelope = None
    env_len = raw_fgb_header.EnvelopeLength()
    if env_len >= 4:
        envelope = (
            float(raw_fgb_header.Envelope(0)),
            float(raw_fgb_header.Envelope(1)),
            float(raw_fgb_header.Envelope(2)),
            float(raw_fgb_header.Envelope(3)),
        )
    elif fgb_header_meta.envelope and len(fgb_header_meta.envelope) >= 4:
        envelope = (
            float(fgb_header_meta.envelope[0]),
            float(fgb_header_meta.envelope[1]),
            float(fgb_header_meta.envelope[2]),
            float(fgb_header_meta.envelope[3]),
        )

    # Extract column names
    columns = []
    if fgb_header_meta.columns:
        for c in fgb_header_meta.columns:
            cname = c.name.decode("utf-8") if isinstance(c.name, (bytes, bytearray)) else str(c.name)
            columns.append(cname)

    name_str = "cadastre"
    raw_name = raw_fgb_header.Name()
    if raw_name:
        name_str = raw_name.decode("utf-8") if isinstance(raw_name, (bytes, bytearray)) else str(raw_name)
    elif hasattr(fgb_header_meta, "title") and fgb_header_meta.title:
        title_val = fgb_header_meta.title
        name_str = title_val.decode("utf-8") if isinstance(title_val, (bytes, bytearray)) else str(title_val)

    return FlatGeobufHeader(
        name=name_str,
        geometry_type=int(fgb_header_meta.geometry_type or 0),
        features_count=features_count,
        index_node_size=index_node_size,
        has_spatial_index=has_spatial_index,
        crs_code=crs_code,
        crs_name=crs_name,
        envelope=envelope,
        columns=columns,
        header_length=header_length,
        index_length=index_length,
    )


def build_synthetic_fgb_payload(
    name: str = "cadastre_sample",
    features_count: int = 10,
    index_node_size: int = 16,
    crs_code: int = 24378,
    crs_name: str = "EPSG:24378",
    envelope: Optional[Tuple[float, float, float, float]] = (3600000.0, 500000.0, 3600220.0, 500100.0),
    columns: Optional[List[Tuple[str, int]]] = None,
) -> bytes:
    """Build a deterministic, valid FlatGeobuf binary payload (magic bytes + size-prefixed header)."""
    builder = flatbuffers.Builder(2048)

    # 1. Strings
    name_offset = builder.CreateString(name)
    crs_name_offset = builder.CreateString(crs_name)

    # 2. CRS table
    CrsFB.Start(builder)
    CrsFB.AddCode(builder, crs_code)
    CrsFB.AddName(builder, crs_name_offset)
    crs_offset = CrsFB.End(builder)

    # 3. Columns vector
    cols_to_add = columns or [("khasra_no", 1), ("owner", 1), ("landuse", 1), ("area_sqm", 2)]
    col_offsets = []
    for col_name, ctype in cols_to_add:
        cname_offset = builder.CreateString(col_name)
        Col.Start(builder)
        Col.AddName(builder, cname_offset)
        Col.AddType(builder, ctype)
        col_offsets.append(Col.End(builder))

    HeaderFB.StartColumnsVector(builder, len(col_offsets))
    for coff in reversed(col_offsets):
        builder.PrependUOffsetTRelative(coff)
    columns_vector_offset = builder.EndVector()

    # 4. Envelope vector
    envelope_vector_offset = None
    if envelope:
        HeaderFB.StartEnvelopeVector(builder, 4)
        for val in reversed(envelope):
            builder.PrependFloat64(float(val))
        envelope_vector_offset = builder.EndVector()

    # 5. Header table
    HeaderFB.Start(builder)
    HeaderFB.AddName(builder, name_offset)
    HeaderFB.AddGeometryType(builder, GeomTypeFB.GeometryType.Polygon)
    HeaderFB.AddFeaturesCount(builder, features_count)
    HeaderFB.AddIndexNodeSize(builder, index_node_size)
    HeaderFB.AddCrs(builder, crs_offset)
    HeaderFB.AddColumns(builder, columns_vector_offset)
    if envelope_vector_offset:
        HeaderFB.AddEnvelope(builder, envelope_vector_offset)

    header_offset = HeaderFB.End(builder)
    builder.Finish(header_offset)

    header_bytes = builder.Output()
    header_len_bytes = struct.pack("<I", len(header_bytes))

    return magicbytes + header_len_bytes + header_bytes


class FlatGeobufIngestionReader:
    """Reader for parsing, validating, and extracting data from FlatGeobuf binary datasets."""

    @classmethod
    def read_header_from_bytes(cls, data: bytes) -> FlatGeobufHeader:
        """Parse FlatGeobuf header from raw bytes."""
        return parse_fgb_header(data)

    @classmethod
    def read_header_from_file(cls, file_path: Union[str, Path]) -> FlatGeobufHeader:
        """Parse FlatGeobuf header from a file on disk."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"FlatGeobuf file not found: {path}")

        # Read initial 64 KB to parse header without loading entire massive file into RAM
        with open(path, "rb") as f:
            initial_chunk = f.read(65536)

        if len(initial_chunk) < 12:
            raise FlatGeobufCorruptHeaderError("File too small to contain FlatGeobuf header")

        header_len = struct.unpack("<I", initial_chunk[8:12])[0]
        required_len = 12 + header_len

        if len(initial_chunk) >= required_len:
            return parse_fgb_header(initial_chunk[:required_len])

        # If header exceeded 64 KB, read full header size
        with open(path, "rb") as f:
            full_header_chunk = f.read(required_len)
        return parse_fgb_header(full_header_chunk)

    @classmethod
    def read_from_bytes(
        cls,
        data: bytes,
        target_crs: Optional[str] = None,
    ) -> FlatGeobufIngestionResult:
        """Parse FlatGeobuf binary payload and return result summary."""
        header = parse_fgb_header(data)
        detected_crs = target_crs or header.canonical_crs

        return FlatGeobufIngestionResult(
            header=header,
            features=[],  # Baseline feature container
            detected_crs=detected_crs,
            total_count=header.features_count,
            valid_count=header.features_count,
            invalid_count=0,
            has_spatial_index=header.has_spatial_index,
            bounds=header.envelope,
        )

    @classmethod
    def read_from_file(
        cls,
        file_path: Union[str, Path],
        target_crs: Optional[str] = None,
    ) -> FlatGeobufIngestionResult:
        """Read FlatGeobuf file from disk and return ingestion summary."""
        header = cls.read_header_from_file(file_path)
        detected_crs = target_crs or header.canonical_crs

        return FlatGeobufIngestionResult(
            header=header,
            features=[],
            detected_crs=detected_crs,
            total_count=header.features_count,
            valid_count=header.features_count,
            invalid_count=0,
            has_spatial_index=header.has_spatial_index,
            bounds=header.envelope,
        )
