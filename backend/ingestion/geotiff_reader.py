"""GeoTIFF ingestion primitives: multi-page TIFF tag extraction and GeoTIFF georeference tag parsing.

Implemented with the Python standard library only (``struct``) so that raster metadata can be
extracted deterministically and offline, without GDAL/rasterio. Supports:

* Classic TIFF (version 42) and BigTIFF (version 43), little- (``II``) and big-endian (``MM``).
* Multi-page files via the IFD chain, with loop and bounds protection.
* GeoTIFF tags: ModelPixelScaleTag (33550), ModelTiepointTag (33922),
  ModelTransformationTag (34264), GeoKeyDirectoryTag (34735), GeoDoubleParamsTag (34736)
  and GeoAsciiParamsTag (34737).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# TIFF / GeoTIFF constants
# ---------------------------------------------------------------------------

TIFF_VERSION_CLASSIC = 42
TIFF_VERSION_BIGTIFF = 43
MAX_TIFF_PAGES = 4096

# TIFF field types: type id -> (struct format per element, byte size per element)
TIFF_TYPE_BYTE = 1
TIFF_TYPE_ASCII = 2
TIFF_TYPE_SHORT = 3
TIFF_TYPE_LONG = 4
TIFF_TYPE_RATIONAL = 5
TIFF_TYPE_SBYTE = 6
TIFF_TYPE_UNDEFINED = 7
TIFF_TYPE_SSHORT = 8
TIFF_TYPE_SLONG = 9
TIFF_TYPE_SRATIONAL = 10
TIFF_TYPE_FLOAT = 11
TIFF_TYPE_DOUBLE = 12
TIFF_TYPE_LONG8 = 16
TIFF_TYPE_SLONG8 = 17
TIFF_TYPE_IFD8 = 18

TIFF_TYPE_FORMATS: Dict[int, Tuple[str, int]] = {
    TIFF_TYPE_BYTE: ("B", 1),
    TIFF_TYPE_ASCII: ("s", 1),
    TIFF_TYPE_SHORT: ("H", 2),
    TIFF_TYPE_LONG: ("I", 4),
    TIFF_TYPE_RATIONAL: ("II", 8),
    TIFF_TYPE_SBYTE: ("b", 1),
    TIFF_TYPE_UNDEFINED: ("B", 1),
    TIFF_TYPE_SSHORT: ("h", 2),
    TIFF_TYPE_SLONG: ("i", 4),
    TIFF_TYPE_SRATIONAL: ("ii", 8),
    TIFF_TYPE_FLOAT: ("f", 4),
    TIFF_TYPE_DOUBLE: ("d", 8),
    TIFF_TYPE_LONG8: ("Q", 8),
    TIFF_TYPE_SLONG8: ("q", 8),
    TIFF_TYPE_IFD8: ("Q", 8),
}

# Baseline TIFF tags
TAG_NEW_SUBFILE_TYPE = 254
TAG_IMAGE_WIDTH = 256
TAG_IMAGE_LENGTH = 257
TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_PHOTOMETRIC = 262
TAG_IMAGE_DESCRIPTION = 270
TAG_STRIP_OFFSETS = 273
TAG_SAMPLES_PER_PIXEL = 277
TAG_ROWS_PER_STRIP = 278
TAG_STRIP_BYTE_COUNTS = 279
TAG_X_RESOLUTION = 282
TAG_Y_RESOLUTION = 283
TAG_RESOLUTION_UNIT = 296
TAG_SAMPLE_FORMAT = 339

# GeoTIFF tags
TAG_MODEL_PIXEL_SCALE = 33550
TAG_MODEL_TIEPOINT = 33922
TAG_MODEL_TRANSFORMATION = 34264
TAG_GEO_KEY_DIRECTORY = 34735
TAG_GEO_DOUBLE_PARAMS = 34736
TAG_GEO_ASCII_PARAMS = 34737

# GeoKeys
GEOKEY_GT_MODEL_TYPE = 1024
GEOKEY_GT_RASTER_TYPE = 1025
GEOKEY_GT_CITATION = 1026
GEOKEY_GEOGRAPHIC_TYPE = 2048
GEOKEY_GEOG_CITATION = 2049
GEOKEY_GEOG_ANGULAR_UNITS = 2054
GEOKEY_GEOG_SEMI_MAJOR_AXIS = 2057
GEOKEY_GEOG_INV_FLATTENING = 2059
GEOKEY_PROJECTED_CS_TYPE = 3072
GEOKEY_PROJ_CITATION = 3073
GEOKEY_PROJ_LINEAR_UNITS = 3076

GEOKEY_NAMES: Dict[int, str] = {
    GEOKEY_GT_MODEL_TYPE: "GTModelTypeGeoKey",
    GEOKEY_GT_RASTER_TYPE: "GTRasterTypeGeoKey",
    GEOKEY_GT_CITATION: "GTCitationGeoKey",
    GEOKEY_GEOGRAPHIC_TYPE: "GeographicTypeGeoKey",
    GEOKEY_GEOG_CITATION: "GeogCitationGeoKey",
    GEOKEY_GEOG_ANGULAR_UNITS: "GeogAngularUnitsGeoKey",
    GEOKEY_GEOG_SEMI_MAJOR_AXIS: "GeogSemiMajorAxisGeoKey",
    GEOKEY_GEOG_INV_FLATTENING: "GeogInvFlatteningGeoKey",
    GEOKEY_PROJECTED_CS_TYPE: "ProjectedCSTypeGeoKey",
    GEOKEY_PROJ_CITATION: "PJCitationGeoKey",
    GEOKEY_PROJ_LINEAR_UNITS: "ProjLinearUnitsGeoKey",
}

MODEL_TYPE_PROJECTED = 1
MODEL_TYPE_GEOGRAPHIC = 2
MODEL_TYPE_GEOCENTRIC = 3
RASTER_PIXEL_IS_AREA = 1
RASTER_PIXEL_IS_POINT = 2
GEOKEY_USER_DEFINED = 32767


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GeoTIFFExtractionError(Exception):
    """Base exception for GeoTIFF extraction errors."""


class GeoTIFFInvalidHeaderError(GeoTIFFExtractionError):
    """Raised when the TIFF byte-order mark, version, or header layout is invalid."""


class GeoTIFFCorruptIFDError(GeoTIFFExtractionError):
    """Raised when an Image File Directory is truncated, out of bounds, or loops."""


class GeoTIFFGeoreferenceError(GeoTIFFExtractionError):
    """Raised when GeoTIFF georeferencing tags are malformed."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TiffHeader:
    """Parsed TIFF file header."""

    byte_order: str  # "<" little-endian (II) or ">" big-endian (MM)
    is_bigtiff: bool
    first_ifd_offset: int

    @property
    def header_size(self) -> int:
        return 16 if self.is_bigtiff else 8


@dataclass(frozen=True)
class TiffTag:
    """Single decoded TIFF directory entry."""

    tag: int
    field_type: int
    count: int
    value: Union[str, Tuple[Any, ...]]

    @property
    def scalar(self) -> Any:
        """First element of a numeric tag, or the string for ASCII tags."""
        if isinstance(self.value, str):
            return self.value
        return self.value[0] if self.value else None


@dataclass(frozen=True)
class ModelTiepoint:
    """Raster (I, J, K) to model (X, Y, Z) tiepoint from ModelTiepointTag."""

    i: float
    j: float
    k: float
    x: float
    y: float
    z: float


@dataclass
class GeoKeyDirectory:
    """Decoded GeoKeyDirectoryTag contents."""

    version: int
    key_revision: int
    minor_revision: int
    keys: Dict[int, Any] = field(default_factory=dict)

    @property
    def model_type(self) -> Optional[int]:
        return self.keys.get(GEOKEY_GT_MODEL_TYPE)

    @property
    def raster_type(self) -> int:
        return int(self.keys.get(GEOKEY_GT_RASTER_TYPE, RASTER_PIXEL_IS_AREA))

    @property
    def is_pixel_is_point(self) -> bool:
        return self.raster_type == RASTER_PIXEL_IS_POINT

    @property
    def citation(self) -> Optional[str]:
        return self.keys.get(GEOKEY_GT_CITATION)

    @property
    def projected_epsg(self) -> Optional[int]:
        code = self.keys.get(GEOKEY_PROJECTED_CS_TYPE)
        return int(code) if code not in (None, 0, GEOKEY_USER_DEFINED) else None

    @property
    def geographic_epsg(self) -> Optional[int]:
        code = self.keys.get(GEOKEY_GEOGRAPHIC_TYPE)
        return int(code) if code not in (None, 0, GEOKEY_USER_DEFINED) else None

    @property
    def epsg(self) -> Optional[int]:
        """Resolve the EPSG code matching the declared model type."""
        if self.model_type == MODEL_TYPE_GEOGRAPHIC:
            return self.geographic_epsg
        if self.projected_epsg is not None:
            return self.projected_epsg
        return self.geographic_epsg

    @property
    def canonical_crs(self) -> Optional[str]:
        code = self.epsg
        return f"EPSG:{code}" if code is not None else None

    def named_keys(self) -> Dict[str, Any]:
        """Keys mapped to their GeoTIFF specification names (unknown ids kept numeric)."""
        return {GEOKEY_NAMES.get(k, str(k)): v for k, v in sorted(self.keys.items())}


@dataclass
class TiffPage:
    """One Image File Directory (page) of a TIFF file."""

    index: int
    offset: int
    tags: Dict[int, TiffTag]

    def get(self, tag: int, default: Any = None) -> Any:
        entry = self.tags.get(tag)
        return entry.value if entry is not None else default

    def _scalar(self, tag: int, default: Any = None) -> Any:
        entry = self.tags.get(tag)
        return entry.scalar if entry is not None else default

    @property
    def width(self) -> int:
        return int(self._scalar(TAG_IMAGE_WIDTH, 0))

    @property
    def height(self) -> int:
        return int(self._scalar(TAG_IMAGE_LENGTH, 0))

    @property
    def samples_per_pixel(self) -> int:
        return int(self._scalar(TAG_SAMPLES_PER_PIXEL, 1))

    @property
    def bits_per_sample(self) -> Tuple[int, ...]:
        value = self.get(TAG_BITS_PER_SAMPLE)
        return tuple(int(v) for v in value) if value else (1,)

    @property
    def compression(self) -> int:
        return int(self._scalar(TAG_COMPRESSION, 1))

    @property
    def photometric(self) -> Optional[int]:
        value = self._scalar(TAG_PHOTOMETRIC)
        return int(value) if value is not None else None

    @property
    def new_subfile_type(self) -> int:
        return int(self._scalar(TAG_NEW_SUBFILE_TYPE, 0))

    @property
    def is_reduced_resolution(self) -> bool:
        return bool(self.new_subfile_type & 0x1)

    @property
    def description(self) -> Optional[str]:
        return self._scalar(TAG_IMAGE_DESCRIPTION)

    @property
    def has_georeference(self) -> bool:
        return TAG_MODEL_TRANSFORMATION in self.tags or (
            TAG_MODEL_TIEPOINT in self.tags
            and (TAG_MODEL_PIXEL_SCALE in self.tags or len(self.get(TAG_MODEL_TIEPOINT)) > 6)
        )

    @property
    def model_tiepoints(self) -> List[ModelTiepoint]:
        value = self.get(TAG_MODEL_TIEPOINT)
        return parse_model_tiepoints(value) if value is not None else []

    @property
    def model_pixel_scale(self) -> Optional[Tuple[float, float, float]]:
        value = self.get(TAG_MODEL_PIXEL_SCALE)
        return parse_model_pixel_scale(value) if value is not None else None

    @property
    def model_transformation(self) -> Optional[Tuple[float, ...]]:
        value = self.get(TAG_MODEL_TRANSFORMATION)
        return parse_model_transformation(value) if value is not None else None

    @property
    def geokey_directory(self) -> Optional[GeoKeyDirectory]:
        directory = self.get(TAG_GEO_KEY_DIRECTORY)
        if directory is None:
            return None
        return parse_geokey_directory(
            directory,
            double_params=self.get(TAG_GEO_DOUBLE_PARAMS, ()),
            ascii_params=self.get(TAG_GEO_ASCII_PARAMS, ""),
        )


# ---------------------------------------------------------------------------
# Header and IFD parsing
# ---------------------------------------------------------------------------


def parse_tiff_header(data: bytes) -> TiffHeader:
    """Validate the TIFF byte-order mark and version, returning the first IFD offset."""
    if not data or len(data) < 8:
        raise GeoTIFFInvalidHeaderError(
            f"TIFF payload too short: {len(data) if data else 0} bytes (minimum 8 bytes required)"
        )

    mark = bytes(data[:2])
    if mark == b"II":
        byte_order = "<"
    elif mark == b"MM":
        byte_order = ">"
    else:
        raise GeoTIFFInvalidHeaderError(f"Invalid TIFF byte-order mark: {mark!r} (expected b'II' or b'MM')")

    version = struct.unpack(byte_order + "H", data[2:4])[0]
    if version == TIFF_VERSION_CLASSIC:
        first_ifd = struct.unpack(byte_order + "I", data[4:8])[0]
        header = TiffHeader(byte_order=byte_order, is_bigtiff=False, first_ifd_offset=first_ifd)
    elif version == TIFF_VERSION_BIGTIFF:
        if len(data) < 16:
            raise GeoTIFFInvalidHeaderError("BigTIFF payload too short: minimum 16 bytes required")
        offset_size, reserved = struct.unpack(byte_order + "HH", data[4:8])
        if offset_size != 8 or reserved != 0:
            raise GeoTIFFInvalidHeaderError(
                f"Invalid BigTIFF header: offset size {offset_size}, reserved {reserved}"
            )
        first_ifd = struct.unpack(byte_order + "Q", data[8:16])[0]
        header = TiffHeader(byte_order=byte_order, is_bigtiff=True, first_ifd_offset=first_ifd)
    else:
        raise GeoTIFFInvalidHeaderError(f"Unsupported TIFF version: {version} (expected 42 or 43)")

    if header.first_ifd_offset < header.header_size or header.first_ifd_offset >= len(data):
        raise GeoTIFFCorruptIFDError(
            f"First IFD offset {header.first_ifd_offset} outside payload of {len(data)} bytes"
        )
    return header


def _decode_tag_value(raw: bytes, field_type: int, count: int, byte_order: str) -> Union[str, Tuple[Any, ...]]:
    if field_type == TIFF_TYPE_ASCII:
        return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    fmt, _ = TIFF_TYPE_FORMATS[field_type]
    values = struct.unpack(f"{byte_order}{count * len(fmt)}{fmt[0]}", raw)
    if field_type in (TIFF_TYPE_RATIONAL, TIFF_TYPE_SRATIONAL):
        return tuple(
            (values[n] / values[n + 1]) if values[n + 1] else 0.0 for n in range(0, len(values), 2)
        )
    return tuple(values)


def _read_ifd(data: bytes, header: TiffHeader, offset: int) -> Tuple[Dict[int, TiffTag], int]:
    bo = header.byte_order
    if header.is_bigtiff:
        count_fmt, entry_size, inline_cap, off_fmt, count_size = "Q", 20, 8, "Q", 8
    else:
        count_fmt, entry_size, inline_cap, off_fmt, count_size = "H", 12, 4, "I", 2
    offset_size = struct.calcsize(off_fmt)

    if offset + count_size > len(data):
        raise GeoTIFFCorruptIFDError(f"IFD at offset {offset} truncated before entry count")
    num_entries = struct.unpack(bo + count_fmt, data[offset : offset + count_size])[0]
    if num_entries == 0:
        raise GeoTIFFCorruptIFDError(f"IFD at offset {offset} contains zero entries")

    entries_start = offset + count_size
    ifd_end = entries_start + num_entries * entry_size + offset_size
    if ifd_end > len(data):
        raise GeoTIFFCorruptIFDError(
            f"IFD at offset {offset} with {num_entries} entries exceeds payload of {len(data)} bytes"
        )

    entry_fmt = bo + "HH" + ("Q" if header.is_bigtiff else "I")
    tags: Dict[int, TiffTag] = {}
    for n in range(num_entries):
        pos = entries_start + n * entry_size
        tag, field_type, count = struct.unpack(entry_fmt, data[pos : pos + 4 + (8 if header.is_bigtiff else 4)])
        value_field = data[pos + entry_size - inline_cap : pos + entry_size]
        if field_type not in TIFF_TYPE_FORMATS:
            continue  # TIFF 6.0: readers must skip fields with unknown types

        total_size = TIFF_TYPE_FORMATS[field_type][1] * count
        if total_size <= inline_cap:
            raw = bytes(value_field[:total_size])
        else:
            value_offset = struct.unpack(bo + off_fmt, value_field)[0]
            if value_offset + total_size > len(data):
                raise GeoTIFFCorruptIFDError(
                    f"Tag {tag} value ({total_size} bytes at offset {value_offset}) exceeds payload"
                )
            raw = bytes(data[value_offset : value_offset + total_size])
        tags[tag] = TiffTag(tag=tag, field_type=field_type, count=count, value=_decode_tag_value(raw, field_type, count, bo))

    next_offset = struct.unpack(bo + off_fmt, data[ifd_end - offset_size : ifd_end])[0]
    return tags, next_offset


def read_tiff_pages(data: bytes, max_pages: int = MAX_TIFF_PAGES) -> List[TiffPage]:
    """Walk the IFD chain and return every page with its decoded tags."""
    header = parse_tiff_header(data)
    pages: List[TiffPage] = []
    visited: set[int] = set()
    offset = header.first_ifd_offset

    while offset:
        if offset in visited:
            raise GeoTIFFCorruptIFDError(f"IFD chain loop detected at offset {offset}")
        if offset < header.header_size or offset >= len(data):
            raise GeoTIFFCorruptIFDError(f"IFD offset {offset} outside payload of {len(data)} bytes")
        if len(pages) >= max_pages:
            raise GeoTIFFCorruptIFDError(f"TIFF exceeds maximum page count of {max_pages}")
        visited.add(offset)
        tags, next_offset = _read_ifd(data, header, offset)
        pages.append(TiffPage(index=len(pages), offset=offset, tags=tags))
        offset = next_offset

    return pages


def read_tiff_pages_from_file(file_path: Union[str, Path], max_pages: int = MAX_TIFF_PAGES) -> List[TiffPage]:
    """Read and parse every TIFF page from a file on disk."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"GeoTIFF file not found: {path}")
    return read_tiff_pages(path.read_bytes(), max_pages=max_pages)


# ---------------------------------------------------------------------------
# GeoTIFF tag parsing
# ---------------------------------------------------------------------------


def parse_model_tiepoints(values: Sequence[float]) -> List[ModelTiepoint]:
    """Parse ModelTiepointTag doubles into (I, J, K, X, Y, Z) tiepoints."""
    if not values or len(values) % 6 != 0:
        raise GeoTIFFGeoreferenceError(
            f"ModelTiepointTag must contain a positive multiple of 6 values, found {len(values) if values else 0}"
        )
    return [ModelTiepoint(*(float(v) for v in values[n : n + 6])) for n in range(0, len(values), 6)]


def parse_model_pixel_scale(values: Sequence[float]) -> Tuple[float, float, float]:
    """Parse ModelPixelScaleTag (ScaleX, ScaleY, ScaleZ)."""
    if not values or len(values) != 3:
        raise GeoTIFFGeoreferenceError(
            f"ModelPixelScaleTag must contain exactly 3 values, found {len(values) if values else 0}"
        )
    sx, sy, sz = (float(v) for v in values)
    if sx == 0.0 or sy == 0.0:
        raise GeoTIFFGeoreferenceError(f"ModelPixelScaleTag has degenerate scale ({sx}, {sy})")
    return sx, sy, sz


def parse_model_transformation(values: Sequence[float]) -> Tuple[float, ...]:
    """Parse ModelTransformationTag 4x4 row-major affine matrix."""
    if not values or len(values) != 16:
        raise GeoTIFFGeoreferenceError(
            f"ModelTransformationTag must contain exactly 16 values, found {len(values) if values else 0}"
        )
    return tuple(float(v) for v in values)


def parse_geokey_directory(
    directory: Sequence[int],
    double_params: Sequence[float] = (),
    ascii_params: str = "",
) -> GeoKeyDirectory:
    """Decode GeoKeyDirectoryTag, resolving values stored in GeoDouble/GeoAscii params."""
    if not directory or len(directory) < 4:
        raise GeoTIFFGeoreferenceError("GeoKeyDirectoryTag header requires at least 4 SHORT values")

    version, key_revision, minor_revision, num_keys = (int(v) for v in directory[:4])
    if version != 1:
        raise GeoTIFFGeoreferenceError(f"Unsupported GeoKeyDirectory version: {version}")
    if len(directory) < 4 + 4 * num_keys:
        raise GeoTIFFGeoreferenceError(
            f"GeoKeyDirectoryTag declares {num_keys} keys but has only {len(directory)} values"
        )

    keys: Dict[int, Any] = {}
    for n in range(num_keys):
        key_id, location, count, value_offset = (int(v) for v in directory[4 + 4 * n : 8 + 4 * n])
        if location == 0:
            keys[key_id] = value_offset
        elif location == TAG_GEO_DOUBLE_PARAMS:
            if value_offset + count > len(double_params):
                raise GeoTIFFGeoreferenceError(f"GeoKey {key_id} references missing GeoDoubleParams values")
            vals = tuple(float(v) for v in double_params[value_offset : value_offset + count])
            keys[key_id] = vals[0] if count == 1 else vals
        elif location == TAG_GEO_ASCII_PARAMS:
            if value_offset + count > len(ascii_params):
                raise GeoTIFFGeoreferenceError(f"GeoKey {key_id} references missing GeoAsciiParams text")
            keys[key_id] = ascii_params[value_offset : value_offset + count].rstrip("|\x00")
        elif location == TAG_GEO_KEY_DIRECTORY:
            if value_offset + count > len(directory):
                raise GeoTIFFGeoreferenceError(f"GeoKey {key_id} references missing GeoKeyDirectory values")
            keys[key_id] = tuple(int(v) for v in directory[value_offset : value_offset + count])
        else:
            raise GeoTIFFGeoreferenceError(f"GeoKey {key_id} has unsupported TIFF tag location {location}")

    return GeoKeyDirectory(version=version, key_revision=key_revision, minor_revision=minor_revision, keys=keys)


# ---------------------------------------------------------------------------
# Deterministic synthetic GeoTIFF builder (offline test payloads)
# ---------------------------------------------------------------------------


@dataclass
class SyntheticGeoTIFFPage:
    """Specification of one page for :func:`build_synthetic_geotiff`."""

    width: int = 4
    height: int = 3
    tiepoints: Optional[List[Tuple[float, float, float, float, float, float]]] = field(
        default_factory=lambda: [(0.0, 0.0, 0.0, 3600000.0, 500100.0, 0.0)]
    )
    pixel_scale: Optional[Tuple[float, float, float]] = (0.5, 0.5, 0.0)
    transformation: Optional[Tuple[float, ...]] = None
    epsg: Optional[int] = 24378
    model_type: int = MODEL_TYPE_PROJECTED
    raster_type: int = RASTER_PIXEL_IS_AREA
    citation: Optional[str] = None
    geo_double_keys: Dict[int, float] = field(default_factory=dict)
    bits_per_sample: int = 8
    samples_per_pixel: int = 1
    compression: int = 1
    new_subfile_type: int = 0
    description: Optional[str] = None


def _encode_values(field_type: int, values: Any, byte_order: str) -> Tuple[int, bytes]:
    if field_type == TIFF_TYPE_ASCII:
        payload = str(values).encode("ascii") + b"\x00"
        return len(payload), payload
    values = list(values)
    if field_type in (TIFF_TYPE_RATIONAL, TIFF_TYPE_SRATIONAL):
        flat: List[int] = []
        for v in values:
            frac = Fraction(str(v)).limit_denominator(0x7FFFFFFF)
            flat.extend((frac.numerator, frac.denominator))
        fmt = TIFF_TYPE_FORMATS[field_type][0][0]
        return len(values), struct.pack(f"{byte_order}{len(flat)}{fmt}", *flat)
    fmt = TIFF_TYPE_FORMATS[field_type][0]
    return len(values), struct.pack(f"{byte_order}{len(values)}{fmt}", *values)


def _page_entries(page: SyntheticGeoTIFFPage, strip_offset: int, strip_size: int, bigtiff: bool) -> List[Tuple[int, int, Any]]:
    offset_type = TIFF_TYPE_LONG8 if bigtiff else TIFF_TYPE_LONG
    entries: List[Tuple[int, int, Any]] = [
        (TAG_NEW_SUBFILE_TYPE, TIFF_TYPE_LONG, [page.new_subfile_type]),
        (TAG_IMAGE_WIDTH, TIFF_TYPE_LONG, [page.width]),
        (TAG_IMAGE_LENGTH, TIFF_TYPE_LONG, [page.height]),
        (TAG_BITS_PER_SAMPLE, TIFF_TYPE_SHORT, [page.bits_per_sample] * page.samples_per_pixel),
        (TAG_COMPRESSION, TIFF_TYPE_SHORT, [page.compression]),
        (TAG_PHOTOMETRIC, TIFF_TYPE_SHORT, [1 if page.samples_per_pixel == 1 else 2]),
        (TAG_STRIP_OFFSETS, offset_type, [strip_offset]),
        (TAG_SAMPLES_PER_PIXEL, TIFF_TYPE_SHORT, [page.samples_per_pixel]),
        (TAG_ROWS_PER_STRIP, TIFF_TYPE_LONG, [page.height]),
        (TAG_STRIP_BYTE_COUNTS, offset_type, [strip_size]),
        (TAG_X_RESOLUTION, TIFF_TYPE_RATIONAL, [72]),
        (TAG_Y_RESOLUTION, TIFF_TYPE_RATIONAL, [72]),
        (TAG_RESOLUTION_UNIT, TIFF_TYPE_SHORT, [2]),
        (TAG_SAMPLE_FORMAT, TIFF_TYPE_SHORT, [1] * page.samples_per_pixel),
    ]
    if page.description:
        entries.append((TAG_IMAGE_DESCRIPTION, TIFF_TYPE_ASCII, page.description))
    if page.pixel_scale is not None:
        entries.append((TAG_MODEL_PIXEL_SCALE, TIFF_TYPE_DOUBLE, list(page.pixel_scale)))
    if page.tiepoints:
        entries.append((TAG_MODEL_TIEPOINT, TIFF_TYPE_DOUBLE, [v for tp in page.tiepoints for v in tp]))
    if page.transformation is not None:
        entries.append((TAG_MODEL_TRANSFORMATION, TIFF_TYPE_DOUBLE, list(page.transformation)))

    if page.epsg is not None or page.citation or page.geo_double_keys:
        keys: List[Tuple[int, int, int, int]] = [
            (GEOKEY_GT_MODEL_TYPE, 0, 1, page.model_type),
            (GEOKEY_GT_RASTER_TYPE, 0, 1, page.raster_type),
        ]
        ascii_params = ""
        if page.citation:
            ascii_params = page.citation + "|"
            keys.append((GEOKEY_GT_CITATION, TAG_GEO_ASCII_PARAMS, len(ascii_params), 0))
        if page.epsg is not None:
            crs_key = GEOKEY_GEOGRAPHIC_TYPE if page.model_type == MODEL_TYPE_GEOGRAPHIC else GEOKEY_PROJECTED_CS_TYPE
            keys.append((crs_key, 0, 1, page.epsg))
        doubles: List[float] = []
        for key_id, value in sorted(page.geo_double_keys.items()):
            keys.append((key_id, TAG_GEO_DOUBLE_PARAMS, 1, len(doubles)))
            doubles.append(float(value))
        keys.sort(key=lambda k: k[0])
        directory = [1, 1, 0, len(keys)] + [v for key in keys for v in key]
        entries.append((TAG_GEO_KEY_DIRECTORY, TIFF_TYPE_SHORT, directory))
        if doubles:
            entries.append((TAG_GEO_DOUBLE_PARAMS, TIFF_TYPE_DOUBLE, doubles))
        if ascii_params:
            entries.append((TAG_GEO_ASCII_PARAMS, TIFF_TYPE_ASCII, ascii_params))

    entries.sort(key=lambda e: e[0])
    return entries


def build_synthetic_geotiff(
    pages: Optional[Sequence[SyntheticGeoTIFFPage]] = None,
    byte_order: str = "<",
    bigtiff: bool = False,
) -> bytes:
    """Build a deterministic, spec-compliant (Big)TIFF payload with GeoTIFF tags and strip data."""
    if byte_order not in ("<", ">"):
        raise ValueError("byte_order must be '<' (II) or '>' (MM)")
    page_specs = list(pages) if pages else [SyntheticGeoTIFFPage()]
    bo = byte_order
    header_size = 16 if bigtiff else 8
    entry_size = 20 if bigtiff else 12
    inline_cap = 8 if bigtiff else 4
    count_fmt = "Q" if bigtiff else "H"
    entry_count_fmt = "Q" if bigtiff else "I"
    off_fmt = "Q" if bigtiff else "I"

    out = bytearray(header_size)
    for idx, page in enumerate(page_specs):
        strip_size = page.width * page.height * page.samples_per_pixel * max(1, page.bits_per_sample // 8)
        strip = bytes((n * 7 + idx) % 256 for n in range(strip_size))

        ifd_offset = len(out)
        draft = _page_entries(page, 0, strip_size, bigtiff)
        ifd_size = struct.calcsize(count_fmt) + len(draft) * entry_size + struct.calcsize(off_fmt)
        ool_total = 0
        for _, ftype, vals in draft:
            payload_len = len(_encode_values(ftype, vals, bo)[1])
            if payload_len > inline_cap:
                ool_total += payload_len + (payload_len & 1)
        strip_offset = ifd_offset + ifd_size + ool_total

        ifd = bytearray(struct.pack(bo + count_fmt, len(draft)))
        ool = bytearray()
        for tag, ftype, vals in _page_entries(page, strip_offset, strip_size, bigtiff):
            count, payload = _encode_values(ftype, vals, bo)
            ifd += struct.pack(bo + "HH" + entry_count_fmt, tag, ftype, count)
            if len(payload) <= inline_cap:
                ifd += payload.ljust(inline_cap, b"\x00")
            else:
                ifd += struct.pack(bo + off_fmt, ifd_offset + ifd_size + len(ool))
                ool += payload
                if len(ool) & 1:
                    ool += b"\x00"

        page_end = strip_offset + len(strip)
        pad = page_end & 1
        next_offset = 0 if idx == len(page_specs) - 1 else page_end + pad
        ifd += struct.pack(bo + off_fmt, next_offset)
        out += ifd + ool + strip + (b"\x00" * pad)

    mark = b"II" if bo == "<" else b"MM"
    if bigtiff:
        out[0:16] = mark + struct.pack(bo + "HHHQ", TIFF_VERSION_BIGTIFF, 8, 0, header_size)[0:14]
    else:
        out[0:8] = mark + struct.pack(bo + "HI", TIFF_VERSION_CLASSIC, header_size)
    return bytes(out)
