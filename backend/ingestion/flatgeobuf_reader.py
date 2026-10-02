"""FlatGeobuf binary ingestion reader: header parsing, CRS identification, and spatial index reading."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
from pathlib import Path
import struct
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

import flatbuffers
from flatgeobuf.constants import SIZE_PREFIX_LEN, magicbytes
import flatgeobuf.FlatGeobuf.Column as Col
import flatgeobuf.FlatGeobuf.ColumnType as ColType
import flatgeobuf.FlatGeobuf.Crs as CrsFB
import flatgeobuf.FlatGeobuf.Feature as FeatureFB
import flatgeobuf.FlatGeobuf.Geometry as GeomFB
import flatgeobuf.FlatGeobuf.GeometryType as GeomTypeFB
import flatgeobuf.FlatGeobuf.Header as HeaderFB
from flatgeobuf.header_meta import from_byte_buffer
from flatgeobuf.packedrtree import PackedRTree, calc_tree_size, generate_level_bounds
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

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
    column_types: List[int] = field(default_factory=list)

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

    # Extract column names and types
    columns = []
    column_types = []
    if fgb_header_meta.columns:
        for c in fgb_header_meta.columns:
            cname = c.name.decode("utf-8") if isinstance(c.name, (bytes, bytearray)) else str(c.name)
            columns.append(cname)
            column_types.append(int(c.type))

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
        column_types=column_types,
    )


def decode_feature_properties(
    feat: FeatureFB.Feature,
    columns: List[str],
    column_types: List[int],
) -> Dict[str, Any]:
    """Decode FlatGeobuf binary properties into a python dictionary."""
    properties: Dict[str, Any] = {}
    if not columns or feat.PropertiesLength() == 0:
        return properties

    props_raw = feat.PropertiesAsNumpy().tobytes()
    if not props_raw:
        return properties

    view = memoryview(props_raw)
    length = len(view)
    offset = 0

    while offset + 2 <= length:
        col_idx = view[offset : offset + 2].cast("H")[0]
        offset += 2
        if col_idx >= len(columns):
            break

        cname = columns[col_idx]
        ctype = column_types[col_idx] if col_idx < len(column_types) else ColType.ColumnType.String

        if ctype == ColType.ColumnType.Bool:
            if offset + 1 > length:
                break
            properties[cname] = bool(view[offset].cast("b")[0])
            offset += 1
        elif ctype in (ColType.ColumnType.Byte, ColType.ColumnType.UByte):
            if offset + 1 > length:
                break
            properties[cname] = int(view[offset].cast("B")[0])
            offset += 1
        elif ctype in (ColType.ColumnType.Short, ColType.ColumnType.UShort):
            if offset + 2 > length:
                break
            properties[cname] = int(view[offset : offset + 2].cast("h")[0])
            offset += 2
        elif ctype in (ColType.ColumnType.Int, ColType.ColumnType.UInt):
            if offset + 4 > length:
                break
            properties[cname] = int(view[offset : offset + 4].cast("i")[0])
            offset += 4
        elif ctype in (ColType.ColumnType.Long, ColType.ColumnType.ULong):
            if offset + 8 > length:
                break
            properties[cname] = int(view[offset : offset + 8].cast("q")[0])
            offset += 8
        elif ctype == ColType.ColumnType.Float:
            if offset + 4 > length:
                break
            properties[cname] = float(view[offset : offset + 4].cast("f")[0])
            offset += 4
        elif ctype == ColType.ColumnType.Double:
            if offset + 8 > length:
                break
            properties[cname] = float(view[offset : offset + 8].cast("d")[0])
            offset += 8
        elif ctype in (ColType.ColumnType.DateTime, ColType.ColumnType.String):
            if offset + 4 > length:
                break
            str_len = view[offset : offset + 4].cast("I")[0]
            offset += 4
            if offset + str_len > length:
                break
            str_val = view[offset : offset + str_len].tobytes().decode("utf-8", errors="replace")
            properties[cname] = str_val
            offset += str_len
        elif ctype == ColType.ColumnType.Json:
            if offset + 4 > length:
                break
            str_len = view[offset : offset + 4].cast("I")[0]
            offset += 4
            if offset + str_len > length:
                break
            json_str = view[offset : offset + str_len].tobytes().decode("utf-8", errors="replace")
            try:
                import json
                properties[cname] = json.loads(json_str)
            except Exception:
                properties[cname] = json_str
            offset += str_len
        else:
            break

    return properties


def decode_feature_geometry(geom: GeomFB.Geometry, header_geom_type: int) -> Optional[Any]:
    """Decode FlatBuffers Geometry table into a Shapely geometry."""
    if not geom:
        return None

    geom_type = header_geom_type if header_geom_type != GeomTypeFB.GeometryType.Unknown else geom.Type()

    if geom_type == GeomTypeFB.GeometryType.Point:
        if geom.XyLength() >= 2:
            return Point(geom.Xy(0), geom.Xy(1))
        return None

    elif geom_type == GeomTypeFB.GeometryType.LineString:
        num_coords = geom.XyLength() // 2
        pts = [(geom.Xy(2 * i), geom.Xy(2 * i + 1)) for i in range(num_coords)]
        return LineString(pts) if len(pts) >= 2 else None

    elif geom_type == GeomTypeFB.GeometryType.Polygon:
        num_coords = geom.XyLength() // 2
        pts = [(geom.Xy(2 * i), geom.Xy(2 * i + 1)) for i in range(num_coords)]
        if not pts:
            return None

        ends_len = geom.EndsLength()
        if ends_len == 0 or ends_len == 1:
            return Polygon(pts)
        else:
            ext_end = geom.Ends(0)
            exterior = pts[:ext_end]
            holes = []
            for j in range(1, ends_len):
                prev_end = geom.Ends(j - 1)
                curr_end = geom.Ends(j)
                holes.append(pts[prev_end:curr_end])
            return Polygon(exterior, holes)

    elif geom_type == GeomTypeFB.GeometryType.MultiPolygon:
        parts_len = geom.PartsLength()
        if parts_len > 0:
            polys = []
            for p_idx in range(parts_len):
                part = geom.Parts(p_idx)
                p_poly = decode_feature_geometry(part, GeomTypeFB.GeometryType.Polygon)
                if p_poly and isinstance(p_poly, Polygon):
                    polys.append(p_poly)
            return MultiPolygon(polys) if polys else None
        else:
            poly = decode_feature_geometry(geom, GeomTypeFB.GeometryType.Polygon)
            return MultiPolygon([poly]) if poly and isinstance(poly, Polygon) else poly

    return None


def build_packed_r_tree(
    items: List[Tuple[float, float, float, float, int]],
    node_size: int = 16,
) -> bytes:
    """Build a deterministic packed Hilbert R-tree index byte buffer according to FlatGeobuf spec.

    Each item is (min_x, min_y, max_x, max_y, feature_byte_offset).
    """
    num_items = len(items)
    if num_items == 0:
        return b""

    level_bounds = generate_level_bounds(num_items, node_size)
    total_nodes = level_bounds[0][1]
    nodes: List[Optional[bytes]] = [None] * total_nodes

    # Leaf nodes are at level 0
    leaf_start, leaf_end = level_bounds[0]
    for i, it in enumerate(items):
        nodes[leaf_start + i] = struct.pack("<ddddq", it[0], it[1], it[2], it[3], it[4])

    # Build parent tree levels bottom-up
    for lvl in range(1, len(level_bounds)):
        child_start, child_end = level_bounds[lvl - 1]
        parent_start, parent_end = level_bounds[lvl]
        for p_idx in range(parent_start, parent_end):
            c_first = child_start + (p_idx - parent_start) * node_size
            c_last = min(c_first + node_size, child_end)

            min_x = min(struct.unpack("<d", nodes[c][0:8])[0] for c in range(c_first, c_last))
            min_y = min(struct.unpack("<d", nodes[c][8:16])[0] for c in range(c_first, c_last))
            max_x = max(struct.unpack("<d", nodes[c][16:24])[0] for c in range(c_first, c_last))
            max_y = max(struct.unpack("<d", nodes[c][24:32])[0] for c in range(c_first, c_last))
            nodes[p_idx] = struct.pack("<ddddq", min_x, min_y, max_x, max_y, c_first)

    return b"".join(n for n in nodes if n is not None)


def serialize_feature(
    geometry: Union[Polygon, MultiPolygon],
    properties: Dict[str, Any],
    columns: List[Tuple[str, int]],
) -> bytes:
    """Serialize a Shapely Polygon / MultiPolygon and properties into FlatGeobuf binary feature bytes."""
    builder = flatbuffers.Builder(1024)

    # 1. Geometry
    if isinstance(geometry, Polygon):
        ext_coords = list(geometry.exterior.coords)
        int_coords = [list(r.coords) for r in geometry.interiors]
        all_rings = [ext_coords] + int_coords
        flat_coords = [coord for ring in all_rings for pt in ring for coord in pt]
        ends = []
        c = 0
        for ring in all_rings:
            c += len(ring)
            ends.append(c)

        GeomFB.GeometryStartXyVector(builder, len(flat_coords))
        for val in reversed(flat_coords):
            builder.PrependFloat64(float(val))
        xy_vec = builder.EndVector()

        GeomFB.GeometryStartEndsVector(builder, len(ends))
        for end_val in reversed(ends):
            builder.PrependUint32(int(end_val))
        ends_vec = builder.EndVector()

        GeomFB.GeometryStart(builder)
        GeomFB.GeometryAddType(builder, GeomTypeFB.GeometryType.Polygon)
        GeomFB.GeometryAddXy(builder, xy_vec)
        GeomFB.GeometryAddEnds(builder, ends_vec)
        geom_offset = GeomFB.GeometryEnd(builder)
    else:
        GeomFB.GeometryStart(builder)
        geom_offset = GeomFB.GeometryEnd(builder)

    # 2. Properties
    prop_buf = bytearray()
    for col_idx, (cname, ctype) in enumerate(columns):
        if cname in properties:
            prop_buf += struct.pack("<H", col_idx)
            val = properties[cname]
            if ctype in (ColType.ColumnType.String, ColType.ColumnType.DateTime):
                s_bytes = str(val).encode("utf-8")
                prop_buf += struct.pack("<I", len(s_bytes)) + s_bytes
            elif ctype == ColType.ColumnType.Double:
                prop_buf += struct.pack("<d", float(val))
            elif ctype in (ColType.ColumnType.Int, ColType.ColumnType.UInt):
                prop_buf += struct.pack("<i", int(val))
            elif ctype == ColType.ColumnType.Bool:
                prop_buf += struct.pack("<b", 1 if val else 0)

    prop_vec = builder.CreateByteVector(bytes(prop_buf))

    # 3. Feature table
    FeatureFB.FeatureStart(builder)
    FeatureFB.FeatureAddGeometry(builder, geom_offset)
    FeatureFB.FeatureAddProperties(builder, prop_vec)
    feat_offset = FeatureFB.FeatureEnd(builder)
    builder.Finish(feat_offset)

    raw_feat = builder.Output()
    return struct.pack("<I", len(raw_feat)) + raw_feat


_DEFAULT_SYNTHETIC_ENVELOPE = (3600000.0, 500000.0, 3600220.0, 500100.0)
_SENTINEL = object()


def build_synthetic_fgb_payload(
    name: str = "cadastre_sample",
    features_count: Optional[int] = None,
    index_node_size: int = 16,
    crs_code: int = 24378,
    crs_name: str = "EPSG:24378",
    envelope: Any = _SENTINEL,
    columns: Optional[List[Tuple[str, int]]] = None,
    features_data: Optional[List[Tuple[Polygon, Dict[str, Any]]]] = None,
) -> bytes:
    """Build a deterministic, valid FlatGeobuf binary payload with header, packed R-tree, and features."""
    builder = flatbuffers.Builder(2048)

    # Prepare feature and column configuration
    cols_to_add = columns or [
        ("khasra_no", ColType.ColumnType.String),
        ("owner_name", ColType.ColumnType.String),
        ("area_sqm", ColType.ColumnType.Double),
    ]

    features_raw_bytes: List[bytes] = []
    rtree_items: List[Tuple[float, float, float, float, int]] = []
    if envelope is _SENTINEL:
        calculated_envelope = None if features_data else _DEFAULT_SYNTHETIC_ENVELOPE
    else:
        calculated_envelope = envelope

    if features_data:
        total_feats = len(features_data)
        eff_count = total_feats
        # Serialize features and accumulate R-tree items
        curr_offset = 0
        minx_list, miny_list, maxx_list, maxy_list = [], [], [], []
        for poly, props in features_data:
            feat_bytes = serialize_feature(poly, props, cols_to_add)
            features_raw_bytes.append(feat_bytes)
            pb = poly.bounds
            minx_list.append(pb[0])
            miny_list.append(pb[1])
            maxx_list.append(pb[2])
            maxy_list.append(pb[3])
            rtree_items.append((pb[0], pb[1], pb[2], pb[3], curr_offset))
            curr_offset += len(feat_bytes)

        if not calculated_envelope and minx_list:
            calculated_envelope = (min(minx_list), min(miny_list), max(maxx_list), max(maxy_list))
    else:
        eff_count = 10 if features_count is None else features_count

    # 1. Strings
    name_offset = builder.CreateString(name)
    crs_name_offset = builder.CreateString(crs_name)

    # 2. CRS table
    CrsFB.Start(builder)
    CrsFB.AddCode(builder, crs_code)
    CrsFB.AddName(builder, crs_name_offset)
    crs_offset = CrsFB.End(builder)

    # 3. Columns vector
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
    if calculated_envelope:
        HeaderFB.StartEnvelopeVector(builder, 4)
        for val in reversed(calculated_envelope):
            builder.PrependFloat64(float(val))
        envelope_vector_offset = builder.EndVector()

    # 5. Header table
    HeaderFB.Start(builder)
    HeaderFB.AddName(builder, name_offset)
    HeaderFB.AddGeometryType(builder, GeomTypeFB.GeometryType.Polygon)
    HeaderFB.AddFeaturesCount(builder, eff_count)
    HeaderFB.AddIndexNodeSize(builder, index_node_size)
    HeaderFB.AddCrs(builder, crs_offset)
    HeaderFB.AddColumns(builder, columns_vector_offset)
    if envelope_vector_offset:
        HeaderFB.AddEnvelope(builder, envelope_vector_offset)

    header_offset = HeaderFB.End(builder)
    builder.Finish(header_offset)

    header_bytes = builder.Output()
    header_len_bytes = struct.pack("<I", len(header_bytes))

    header_payload = magicbytes + header_len_bytes + header_bytes

    # Build packed R-tree index if features present and index_node_size > 0
    tree_payload = b""
    if rtree_items and index_node_size > 0:
        tree_payload = build_packed_r_tree(rtree_items, node_size=index_node_size)

    return header_payload + tree_payload + b"".join(features_raw_bytes)


class FlatGeobufIngestionReader:
    """Reader for parsing, validating, and extracting data from FlatGeobuf binary datasets with R-tree support."""

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

        with open(path, "rb") as f:
            initial_chunk = f.read(65536)

        if len(initial_chunk) < 12:
            raise FlatGeobufCorruptHeaderError("File too small to contain FlatGeobuf header")

        header_len = struct.unpack("<I", initial_chunk[8:12])[0]
        required_len = 12 + header_len

        if len(initial_chunk) >= required_len:
            return parse_fgb_header(initial_chunk[:required_len])

        with open(path, "rb") as f:
            full_header_chunk = f.read(required_len)
        return parse_fgb_header(full_header_chunk)

    @classmethod
    def _parse_single_feature(
        cls,
        feat_bytes: bytes,
        header: FlatGeobufHeader,
        crs_str: str,
        is_geographic: bool,
        fallback_id: str,
    ) -> Optional[FlatGeobufFeatureRecord]:
        """Decode and validate a single binary FlatGeobuf feature record."""
        try:
            feat = FeatureFB.Feature.GetRootAsFeature(bytearray(feat_bytes), 0)
        except Exception:
            return None

        geom_fb = feat.Geometry()
        if not geom_fb:
            return None

        geom = decode_feature_geometry(geom_fb, header.geometry_type)
        if not geom:
            return None

        # Standardize geometry: remove duplicate consecutive vertices, winding order, precision
        if isinstance(geom, (Polygon, MultiPolygon)):
            geom = remove_duplicate_consecutive_vertices(geom)
            geom, _ = enforce_rfc7946_winding_order(geom)
        precision = 7 if is_geographic else 3
        geom = round_coordinates(geom, precision=precision)

        # Decode properties and normalize cadastral keys
        props = decode_feature_properties(feat, header.columns, header.column_types)
        norm_props = normalize_cadastral_properties(props)

        feat_id = (
            str(norm_props.get("khasra_no"))
            if norm_props.get("khasra_no")
            else (str(norm_props.get("parcel_id")) if norm_props.get("parcel_id") else fallback_id)
        )

        is_valid = bool(geom.is_valid)
        bounds = tuple(geom.bounds)
        area = float(geom.area) if hasattr(geom, "area") else 0.0

        return FlatGeobufFeatureRecord(
            feature_id=feat_id,
            geometry=geom,
            properties=norm_props,
            crs=crs_str,
            is_valid=is_valid,
            bounds=bounds,
            area=area,
        )

    @classmethod
    def iter_features_from_stream(
        cls,
        stream: io.BufferedIOBase,
        header: FlatGeobufHeader,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        target_crs: Optional[str] = None,
    ) -> Generator[FlatGeobufFeatureRecord, None, None]:
        """Stream features from an open FlatGeobuf byte stream, using packed R-tree index when available."""
        tree_offset = 12 + header.header_length
        features_offset = tree_offset + header.index_length
        crs_str = target_crs or header.canonical_crs
        is_geographic = ("4326" in crs_str or "4269" in crs_str or "CRS84" in crs_str)

        if bbox and header.has_spatial_index and header.features_count > 0:
            # Spatial filter using Hilbert R-tree index
            rect = [bbox[0], bbox[1], bbox[2], bbox[3]]

            def read_node(offset_into_tree: int, size: int) -> bytes:
                stream.seek(tree_offset + offset_into_tree)
                return stream.read(size)

            rtree = PackedRTree(header.features_count, header.index_node_size, rect)
            search_results = list(rtree.stream_search(read_node))

            for feature_byte_offset, feat_idx, _ in search_results:
                stream.seek(features_offset + feature_byte_offset)
                len_bytes = stream.read(4)
                if len(len_bytes) < 4:
                    break
                feat_len = struct.unpack("<I", len_bytes)[0]
                feat_bytes = stream.read(feat_len)
                record = cls._parse_single_feature(feat_bytes, header, crs_str, is_geographic, fallback_id=str(feat_idx))
                if record:
                    b = record.bounds
                    # Check envelope overlap
                    if not (b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3]):
                        yield record

        elif bbox and not header.has_spatial_index:
            # Sequential scan with bounding box filtering
            stream.seek(features_offset)
            for feat_idx in range(header.features_count):
                len_bytes = stream.read(4)
                if len(len_bytes) < 4:
                    break
                feat_len = struct.unpack("<I", len_bytes)[0]
                feat_bytes = stream.read(feat_len)
                record = cls._parse_single_feature(feat_bytes, header, crs_str, is_geographic, fallback_id=str(feat_idx))
                if record:
                    b = record.bounds
                    if not (b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3]):
                        yield record

        else:
            # Sequential scan of all features
            stream.seek(features_offset)
            for feat_idx in range(header.features_count):
                len_bytes = stream.read(4)
                if len(len_bytes) < 4:
                    break
                feat_len = struct.unpack("<I", len_bytes)[0]
                feat_bytes = stream.read(feat_len)
                record = cls._parse_single_feature(feat_bytes, header, crs_str, is_geographic, fallback_id=str(feat_idx))
                if record:
                    yield record

    @classmethod
    def iter_features_from_file(
        cls,
        file_path: Union[str, Path],
        bbox: Optional[Tuple[float, float, float, float]] = None,
        target_crs: Optional[str] = None,
    ) -> Generator[FlatGeobufFeatureRecord, None, None]:
        """Stream features from a FlatGeobuf file on disk with optional spatial filtering."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"FlatGeobuf file not found: {path}")

        header = cls.read_header_from_file(path)
        detected_crs = target_crs or header.canonical_crs

        with open(path, "rb") as f:
            yield from cls.iter_features_from_stream(f, header, bbox=bbox, target_crs=detected_crs)

    @classmethod
    def read_from_bytes(
        cls,
        data: bytes,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        target_crs: Optional[str] = None,
    ) -> FlatGeobufIngestionResult:
        """Parse FlatGeobuf binary payload and return result summary with extracted features."""
        stream = io.BytesIO(data)
        header = parse_fgb_header(data)
        detected_crs = target_crs or header.canonical_crs

        features = list(cls.iter_features_from_stream(stream, header, bbox=bbox, target_crs=detected_crs))
        valid_count = sum(1 for f in features if f.is_valid) if features else (header.features_count if not bbox else 0)
        invalid_count = (len(features) - valid_count) if features else 0

        total_envelope = header.envelope
        if not total_envelope and features:
            minx = min(f.bounds[0] for f in features)
            miny = min(f.bounds[1] for f in features)
            maxx = max(f.bounds[2] for f in features)
            maxy = max(f.bounds[3] for f in features)
            total_envelope = (minx, miny, maxx, maxy)

        return FlatGeobufIngestionResult(
            header=header,
            features=features,
            detected_crs=detected_crs,
            total_count=len(features) if bbox else header.features_count,
            valid_count=valid_count,
            invalid_count=invalid_count,
            has_spatial_index=header.has_spatial_index,
            bounds=total_envelope,
        )

    @classmethod
    def read_from_file(
        cls,
        file_path: Union[str, Path],
        bbox: Optional[Tuple[float, float, float, float]] = None,
        target_crs: Optional[str] = None,
    ) -> FlatGeobufIngestionResult:
        """Read FlatGeobuf file from disk and return ingestion summary with extracted features."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"FlatGeobuf file not found: {path}")

        header = cls.read_header_from_file(path)
        detected_crs = target_crs or header.canonical_crs

        with open(path, "rb") as f:
            features = list(cls.iter_features_from_stream(f, header, bbox=bbox, target_crs=detected_crs))

        valid_count = sum(1 for f in features if f.is_valid) if features else (header.features_count if not bbox else 0)
        invalid_count = (len(features) - valid_count) if features else 0

        total_envelope = header.envelope
        if not total_envelope and features:
            minx = min(f.bounds[0] for f in features)
            miny = min(f.bounds[1] for f in features)
            maxx = max(f.bounds[2] for f in features)
            maxy = max(f.bounds[3] for f in features)
            total_envelope = (minx, miny, maxx, maxy)

        return FlatGeobufIngestionResult(
            header=header,
            features=features,
            detected_crs=detected_crs,
            total_count=len(features) if bbox else header.features_count,
            valid_count=valid_count,
            invalid_count=invalid_count,
            has_spatial_index=header.has_spatial_index,
            bounds=total_envelope,
        )
