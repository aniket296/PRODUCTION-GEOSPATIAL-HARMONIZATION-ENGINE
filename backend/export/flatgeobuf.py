"""FlatGeobuf v3.x binary serializer, Packed R-Tree spatial indexer, and streaming helpers (Step 106/150).

Directive Sections 26-28:
- Serializes harmonized cadastral parcels into spec-compliant FlatGeobuf (`.fgb`) binary format in WGS 84 (`EPSG:4326`)
  with a Hilbert-sorted Packed R-Tree spatial index (`index_node_size = 16`).
- Supports chunked byte streaming (`stream_flatgeobuf_bytes`), metadata summary (`build_flatgeobuf_metadata`), and
  indexed bounding-box queries (`read_flatgeobuf_features`).
- Excludes audit-only merged slivers (`attributes["merged_into_parcel_id"]`) by default unless
  `include_merged_slivers=True`.
"""

from __future__ import annotations

import io
import math
import struct
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import flatbuffers
import flatgeobuf
from flatgeobuf.constants import SIZE_PREFIX_LEN, magicbytes as FGB_MAGIC
from flatgeobuf.FlatGeobuf import Column, Crs, Feature, Geometry, Header
from flatgeobuf.FlatGeobuf.ColumnType import ColumnType
from flatgeobuf.FlatGeobuf.GeometryType import GeometryType
from flatgeobuf.header_meta import from_byte_buffer as parse_header_meta
from flatgeobuf.packedrtree import DEFAULT_NODE_SIZE, NODE_ITEM_BYTE_LEN, generate_level_bounds
from geoalchemy2.shape import to_shape
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from backend.models.parcel import HarmonizedParcel

WGS84_SRID: int = 4326
DEFAULT_CHUNK_SIZE: int = 65_536

_COLUMN_SCHEMA: tuple[tuple[str, int], ...] = (
    ("parcel_id", ColumnType.String),
    ("parcel_identifier", ColumnType.String),
    ("metric_area_sqm", ColumnType.Double),
    ("metric_perimeter_m", ColumnType.Double),
    ("polsby_popper_compactness", ColumnType.Double),
    ("is_sliver", ColumnType.Json),
    ("is_disputed", ColumnType.Json),
    ("dispute_locked", ColumnType.Json),
    ("resolution_status", ColumnType.String),
)


@dataclass(frozen=True, slots=True)
class FlatGeobufExportMetadata:
    """Metadata describing an exported FlatGeobuf binary asset."""

    feature_count: int
    byte_length: int
    srid: int
    bbox: tuple[float, float, float, float] | None
    has_spatial_index: bool


def _is_merged_sliver(parcel: HarmonizedParcel) -> bool:
    attrs = parcel.attributes or {}
    return bool(attrs.get("merged_into_parcel_id"))


def _to_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    return MultiPolygon(polys)


def _hilbert_xy2d(n: int, x: int, y: int) -> int:
    """Compute 2D Hilbert curve index for `(x, y)` in `[0, n) x [0, n)`."""
    d = 0
    s = n // 2
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x
                y = n - 1 - y
            x, y = y, x
        s //= 2
    return d


def _hilbert_key(
    bbox: tuple[float, float, float, float],
    extent: tuple[float, float, float, float],
) -> int:
    min_x, min_y, max_x, max_y = extent
    width = max_x - min_x
    height = max_y - min_y
    cx = 0.5 * (bbox[0] + bbox[2])
    cy = 0.5 * (bbox[1] + bbox[3])
    grid = 65536
    ix = int(((cx - min_x) / width) * (grid - 1)) if width > 0.0 else 0
    iy = int(((cy - min_y) / height) * (grid - 1)) if height > 0.0 else 0
    ix = max(0, min(grid - 1, ix))
    iy = max(0, min(grid - 1, iy))
    return _hilbert_xy2d(grid, ix, iy)


def _encode_properties(parcel: HarmonizedParcel) -> bytes:
    buf = bytearray()

    def _add_str(col_idx: int, value: str) -> None:
        encoded = value.encode("utf-8")
        buf.extend(struct.pack("<HI", col_idx, len(encoded)))
        buf.extend(encoded)

    def _add_double(col_idx: int, value: float | None) -> None:
        if value is None:
            return
        buf.extend(struct.pack("<Hd", col_idx, float(value)))

    def _add_bool(col_idx: int, value: bool) -> None:
        encoded = b"true" if value else b"false"
        buf.extend(struct.pack("<HI", col_idx, len(encoded)))
        buf.extend(encoded)

    _add_str(0, str(parcel.id))
    _add_str(1, parcel.parcel_identifier or "")
    _add_double(2, parcel.metric_area_sqm)
    _add_double(3, parcel.metric_perimeter_m)
    _add_double(4, parcel.polsby_popper_compactness)
    _add_bool(5, bool(parcel.is_sliver))
    _add_bool(6, bool(parcel.is_disputed))
    _add_bool(7, bool(parcel.dispute_locked))
    _add_str(8, parcel.resolution_status or "ORIGINAL")
    return bytes(buf)


def _build_polygon_flatbuffer(builder: flatbuffers.Builder, poly: Polygon) -> int:
    rings = [list(poly.exterior.coords)] + [list(ring.coords) for ring in poly.interiors]
    xy_coords: list[float] = []
    ends: list[int] = []
    running = 0
    for ring in rings:
        for pt in ring:
            xy_coords.append(float(pt[0]))
            xy_coords.append(float(pt[1]))
        running += len(ring)
        ends.append(running)

    Geometry.GeometryStartXyVector(builder, len(xy_coords))
    for val in reversed(xy_coords):
        builder.PrependFloat64(val)
    xy_vec = builder.EndVector()

    ends_vec = None
    if len(ends) > 1:
        Geometry.GeometryStartEndsVector(builder, len(ends))
        for end_idx in reversed(ends):
            builder.PrependUint32(end_idx)
        ends_vec = builder.EndVector()

    Geometry.GeometryStart(builder)
    Geometry.GeometryAddXy(builder, xy_vec)
    if ends_vec is not None:
        Geometry.GeometryAddEnds(builder, ends_vec)
    Geometry.GeometryAddType(builder, GeometryType.Polygon)
    return Geometry.GeometryEnd(builder)


def _encode_feature(parcel: HarmonizedParcel, mp: MultiPolygon) -> bytes:
    builder = flatbuffers.Builder(1024)

    part_offsets = [_build_polygon_flatbuffer(builder, poly) for poly in mp.geoms]
    Geometry.GeometryStartPartsVector(builder, len(part_offsets))
    for off in reversed(part_offsets):
        builder.PrependUOffsetTRelative(off)
    parts_vec = builder.EndVector()

    Geometry.GeometryStart(builder)
    Geometry.GeometryAddParts(builder, parts_vec)
    Geometry.GeometryAddType(builder, GeometryType.MultiPolygon)
    geom_off = Geometry.GeometryEnd(builder)

    prop_bytes = _encode_properties(parcel)
    props_vec = builder.CreateByteVector(prop_bytes)

    Feature.FeatureStart(builder)
    Feature.FeatureAddGeometry(builder, geom_off)
    Feature.FeatureAddProperties(builder, props_vec)
    feat_off = Feature.FeatureEnd(builder)

    builder.FinishSizePrefixed(feat_off)
    return bytes(builder.Output())


def _encode_header(
    *,
    layer_name: str,
    features_count: int,
    index_node_size: int,
    envelope: tuple[float, float, float, float] | None,
) -> bytes:
    builder = flatbuffers.Builder(512)

    name_off = builder.CreateString(layer_name)

    col_offsets: list[int] = []
    for col_name, col_type in _COLUMN_SCHEMA:
        col_name_off = builder.CreateString(col_name)
        Column.ColumnStart(builder)
        Column.ColumnAddName(builder, col_name_off)
        Column.ColumnAddType(builder, col_type)
        col_offsets.append(Column.ColumnEnd(builder))

    Header.HeaderStartColumnsVector(builder, len(col_offsets))
    for off in reversed(col_offsets):
        builder.PrependUOffsetTRelative(off)
    cols_vec = builder.EndVector()

    crs_org = builder.CreateString("EPSG")
    crs_name = builder.CreateString("WGS 84")
    Crs.CrsStart(builder)
    Crs.CrsAddOrg(builder, crs_org)
    Crs.CrsAddCode(builder, WGS84_SRID)
    Crs.CrsAddName(builder, crs_name)
    crs_off = Crs.CrsEnd(builder)

    env_vec = None
    if envelope is not None:
        Header.HeaderStartEnvelopeVector(builder, 4)
        for val in reversed(envelope):
            builder.PrependFloat64(val)
        env_vec = builder.EndVector()

    Header.HeaderStart(builder)
    Header.HeaderAddName(builder, name_off)
    if env_vec is not None:
        Header.HeaderAddEnvelope(builder, env_vec)
    Header.HeaderAddGeometryType(builder, GeometryType.MultiPolygon)
    Header.HeaderAddFeaturesCount(builder, features_count)
    Header.HeaderAddIndexNodeSize(builder, index_node_size)
    Header.HeaderAddColumns(builder, cols_vec)
    Header.HeaderAddCrs(builder, crs_off)
    header_off = Header.HeaderEnd(builder)

    builder.FinishSizePrefixed(header_off)
    return bytes(builder.Output())


def _build_packed_rtree(
    leaf_items: list[tuple[tuple[float, float, float, float], int]],
    node_size: int,
) -> bytes:
    """Build a bottom-up Packed R-Tree buffer matching `flatgeobuf.packedrtree.generate_level_bounds`."""
    num_items = len(leaf_items)
    level_bounds = generate_level_bounds(num_items, node_size)
    total_nodes = level_bounds[0][1]
    nodes: list[tuple[float, float, float, float, int]] = [(0.0, 0.0, 0.0, 0.0, 0)] * total_nodes

    leaf_start, _ = level_bounds[0]
    for idx, (bbox, feature_offset) in enumerate(leaf_items):
        nodes[leaf_start + idx] = (bbox[0], bbox[1], bbox[2], bbox[3], feature_offset)

    for level in range(1, len(level_bounds)):
        child_start, child_end = level_bounds[level - 1]
        parent_start, parent_end = level_bounds[level]
        num_parents = parent_end - parent_start
        for p_idx in range(num_parents):
            first_child = child_start + p_idx * node_size
            last_child = min(first_child + node_size, child_end)
            min_x = min(nodes[c][0] for c in range(first_child, last_child))
            min_y = min(nodes[c][1] for c in range(first_child, last_child))
            max_x = max(nodes[c][2] for c in range(first_child, last_child))
            max_y = max(nodes[c][3] for c in range(first_child, last_child))
            nodes[parent_start + p_idx] = (min_x, min_y, max_x, max_y, first_child)

    buf = bytearray(total_nodes * NODE_ITEM_BYTE_LEN)
    for idx, node in enumerate(nodes):
        struct.pack_into("<ddddq", buf, idx * NODE_ITEM_BYTE_LEN, *node)
    return bytes(buf)


def export_parcels_to_flatgeobuf(
    parcels: Sequence[HarmonizedParcel],
    *,
    layer_name: str = "harmonized_parcels",
    include_merged_slivers: bool = False,
    index_node_size: int = DEFAULT_NODE_SIZE,
) -> bytes:
    """Serialize `HarmonizedParcel` records into a spec-compliant FlatGeobuf byte buffer."""
    active = [p for p in parcels if include_merged_slivers or not _is_merged_sliver(p)]
    if not active:
        header_bytes = _encode_header(
            layer_name=layer_name,
            features_count=0,
            index_node_size=0,
            envelope=None,
        )
        return FGB_MAGIC + header_bytes

    records: list[tuple[HarmonizedParcel, MultiPolygon, tuple[float, float, float, float]]] = []
    min_lon = float("inf")
    min_lat = float("inf")
    max_lon = float("-inf")
    max_lat = float("-inf")

    for parcel in active:
        mp = _to_multipolygon(to_shape(parcel.geometry_wgs84))
        b = mp.bounds
        bbox = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        min_lon = min(min_lon, bbox[0])
        min_lat = min(min_lat, bbox[1])
        max_lon = max(max_lon, bbox[2])
        max_lat = max(max_lat, bbox[3])
        records.append((parcel, mp, bbox))

    extent = (min_lon, min_lat, max_lon, max_lat)
    records.sort(key=lambda rec: (_hilbert_key(rec[2], extent), str(rec[0].id)))

    feature_chunks: list[bytes] = []
    leaf_items: list[tuple[tuple[float, float, float, float], int]] = []
    offset = 0
    for parcel, mp, bbox in records:
        feat_bytes = _encode_feature(parcel, mp)
        leaf_items.append((bbox, offset))
        feature_chunks.append(feat_bytes)
        offset += len(feat_bytes)

    header_bytes = _encode_header(
        layer_name=layer_name,
        features_count=len(records),
        index_node_size=index_node_size,
        envelope=extent,
    )
    tree_bytes = _build_packed_rtree(leaf_items, index_node_size)
    return b"".join([FGB_MAGIC, header_bytes, tree_bytes, *feature_chunks])


def _validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must contain exactly 4 coordinates (min_lon, min_lat, max_lon, max_lat)")
    min_x, min_y, max_x, max_y = (float(v) for v in bbox)
    if not all(math.isfinite(v) for v in (min_x, min_y, max_x, max_y)):
        raise ValueError("bbox coordinates must be finite numbers")
    if min_x > max_x or min_y > max_y:
        raise ValueError("bbox min coordinates must be <= max coordinates")
    return (min_x, min_y, max_x, max_y)


def read_flatgeobuf_features(
    fgb_bytes: bytes,
    *,
    bbox: tuple[float, float, float, float] | None = None,
) -> list[dict[str, Any]]:
    """Read features from a FlatGeobuf byte buffer, optionally filtering via Packed R-Tree bbox."""
    validated_bbox = _validate_bbox(bbox) if bbox is not None else None
    if len(fgb_bytes) < len(FGB_MAGIC) + SIZE_PREFIX_LEN or not fgb_bytes.startswith(FGB_MAGIC):
        raise ValueError("Invalid FlatGeobuf byte buffer")

    header_len = int.from_bytes(fgb_bytes[len(FGB_MAGIC) : len(FGB_MAGIC) + SIZE_PREFIX_LEN], "little")
    header_start = len(FGB_MAGIC) + SIZE_PREFIX_LEN
    header_meta = parse_header_meta(bytearray(fgb_bytes[header_start : header_start + header_len]))
    if header_meta.features_count == 0:
        return []

    fc = flatgeobuf.load(io.BytesIO(fgb_bytes), bbox=validated_bbox)
    return list(fc.get("features", []))


def build_flatgeobuf_metadata(
    parcels: Sequence[HarmonizedParcel],
    fgb_bytes: bytes,
    *,
    include_merged_slivers: bool = False,
) -> FlatGeobufExportMetadata:
    """Build export metadata summary for a FlatGeobuf payload."""
    active = [p for p in parcels if include_merged_slivers or not _is_merged_sliver(p)]
    bbox: tuple[float, float, float, float] | None = None
    if active:
        min_lon = float("inf")
        min_lat = float("inf")
        max_lon = float("-inf")
        max_lat = float("-inf")
        for parcel in active:
            b = to_shape(parcel.geometry_wgs84).bounds
            min_lon = min(min_lon, float(b[0]))
            min_lat = min(min_lat, float(b[1]))
            max_lon = max(max_lon, float(b[2]))
            max_lat = max(max_lat, float(b[3]))
        bbox = (min_lon, min_lat, max_lon, max_lat)

    return FlatGeobufExportMetadata(
        feature_count=len(active),
        byte_length=len(fgb_bytes),
        srid=WGS84_SRID,
        bbox=bbox,
        has_spatial_index=len(active) > 0,
    )


def stream_flatgeobuf_bytes(
    fgb_bytes: bytes,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Yield fixed-size byte chunks from a serialized FlatGeobuf payload for HTTP streaming."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    for offset in range(0, len(fgb_bytes), chunk_size):
        yield fgb_bytes[offset : offset + chunk_size]
