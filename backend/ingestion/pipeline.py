"""Unified ingestion pipeline: format auto-detection and reader dispatching."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import struct
from typing import Any, Dict, List, Optional, Tuple, Union
import zipfile

from shapely.geometry import Polygon

from backend.ingestion.dxf_reader import (
    DXFCorruptFileError,
    DXFEmptyFileError,
    DXFIngestionReader,
    DXFIngestionResult,
)
from backend.ingestion.flatgeobuf_reader import (
    FlatGeobufCorruptHeaderError,
    FlatGeobufIngestionReader,
    FlatGeobufIngestionResult,
    FlatGeobufInvalidMagicError,
)
from backend.ingestion.geojson_reader import (
    GeoJSONCorruptFileError,
    GeoJSONEmptyFileError,
    GeoJSONIngestionReader,
    GeoJSONIngestionResult,
    GeoJSONValidationError,
)
from backend.ingestion.geotiff_reader import (
    GeoTIFFCorruptIFDError,
    GeoTIFFIngestionReader,
    GeoTIFFIngestionResult,
    GeoTIFFInvalidHeaderError,
    GeoTIFFMissingGeoreferenceError,
    RasterMetadata,
)
from backend.ingestion.shapefile_reader import (
    ShapefileCorruptArchiveError,
    ShapefileIngestionReader,
    ShapefileIngestionResult,
    ShapefileMissingComponentError,
    ShapefileValidationError,
)
from backend.models.layer import SupportedFormat


class IngestionPipelineError(Exception):
    """Base exception for ingestion pipeline errors."""
    pass


class UnsupportedFormatError(IngestionPipelineError):
    """Raised when uploaded file format cannot be identified or is unsupported."""
    pass


class CorruptDataError(IngestionPipelineError):
    """Raised when uploaded file contains unparseable or corrupted payload."""
    pass


@dataclass
class UnifiedFeatureRecord:
    """Standardized vector feature extracted from any input geospatial format."""

    id: Optional[str]
    geometry: Any
    properties: Dict[str, Any]
    crs: str
    is_valid: bool
    bounds: Tuple[float, float, float, float]
    area: Optional[float] = None


@dataclass
class UnifiedIngestionResult:
    """Consolidated ingestion output representing vector or raster datasets."""

    format: SupportedFormat
    detected_crs: Optional[str]
    feature_count: int
    features: List[UnifiedFeatureRecord]
    bounds: Optional[Tuple[float, float, float, float]]
    is_raster: bool = False
    raster_metadata: Optional[RasterMetadata] = None
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


def detect_format_from_bytes(
    data: bytes,
    filename: Optional[str] = None,
) -> SupportedFormat:
    """Auto-detect geospatial format from binary contents and optional filename hint."""
    if not data or len(data) == 0:
        raise CorruptDataError("Cannot detect format: payload contains 0 bytes")

    # 1. FlatGeobuf magic bytes: b'fgb\x03fgb\x00'
    if len(data) >= 8 and data[:3] == b"fgb" and data[3] == 3 and data[4:7] == b"fgb":
        return SupportedFormat.FLATGEOBUF

    # 2. GeoTIFF / TIFF magic bytes
    if len(data) >= 4:
        # Classic TIFF: II*\x00 (LE) or MM\x00* (BE)
        # BigTIFF: II+\x00 (LE) or MM\x00+ (BE)
        if (
            data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
            or (data[:2] in (b"II", b"MM") and struct.unpack("<H" if data[:2] == b"II" else ">H", data[2:4])[0] in (42, 43))
        ):
            return SupportedFormat.GEOTIFF

    # 3. ZIP archive: PK\x03\x04 (Shapefile ZIP or KMZ)
    if len(data) >= 4 and data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                namelist = [name.lower() for name in zf.namelist()]
                if any(n.endswith(".shp") for n in namelist):
                    return SupportedFormat.SHAPEFILE
                if any(n.endswith(".kml") for n in namelist):
                    return SupportedFormat.KMZ
        except Exception:
            pass
        # Fallback to extension if valid zip but non-standard structure
        if filename:
            fn_lower = filename.lower()
            if fn_lower.endswith(".zip"):
                return SupportedFormat.SHAPEFILE
            if fn_lower.endswith(".kmz"):
                return SupportedFormat.KMZ

    # 4. Standalone Shapefile binary (.shp file code 9994 = 0x0000270a)
    if len(data) >= 4 and data[:4] == b"\x00\x00\x27\x0a":
        return SupportedFormat.SHAPEFILE

    # 5. AutoCAD Binary DXF: starts with "AutoCAD Binary DXF\r\n\x1a\x00"
    if len(data) >= 22 and data.startswith(b"AutoCAD Binary DXF"):
        return SupportedFormat.DXF

    # 6. Text-based detection: DXF ASCII, GeoJSON, KML
    # Try decoding initial chunk as text
    initial_sample = data[:65536]
    text_sample = None
    for encoding in ("utf-8", "latin-1", "ascii"):
        try:
            text_sample = initial_sample.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text_sample:
        stripped = text_sample.strip()

        # DXF ASCII: 0 followed by SECTION
        if (
            "SECTION" in stripped
            and ("0\nSECTION" in stripped or "0\r\nSECTION" in stripped or stripped.startswith("0"))
        ):
            return SupportedFormat.DXF

        # KML XML
        if "<kml" in stripped.lower() or ("<?xml" in stripped and "<document" in stripped.lower()):
            return SupportedFormat.KML

        # GeoJSON: starts with '{' or '[' and contains GeoJSON indicators
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                # Test parsing if small, or inspect structure
                parsed = json.loads(text_sample if len(data) <= 1048576 else stripped[:4096] + "}")
                if isinstance(parsed, dict) and "type" in parsed:
                    if parsed["type"] in (
                        "FeatureCollection",
                        "Feature",
                        "Polygon",
                        "MultiPolygon",
                        "GeometryCollection",
                        "Point",
                        "LineString",
                    ):
                        return SupportedFormat.GEOJSON
            except Exception:
                # Check for characteristic GeoJSON strings
                if '"FeatureCollection"' in text_sample or '"features"' in text_sample or '"geometry"' in text_sample:
                    return SupportedFormat.GEOJSON

    # 7. Fallback to filename extension
    if filename:
        ext = Path(filename).suffix.lower()
        ext_map = {
            ".geojson": SupportedFormat.GEOJSON,
            ".json": SupportedFormat.GEOJSON,
            ".shp": SupportedFormat.SHAPEFILE,
            ".zip": SupportedFormat.SHAPEFILE,
            ".dxf": SupportedFormat.DXF,
            ".fgb": SupportedFormat.FLATGEOBUF,
            ".tif": SupportedFormat.GEOTIFF,
            ".tiff": SupportedFormat.GEOTIFF,
            ".geotiff": SupportedFormat.GEOTIFF,
            ".kml": SupportedFormat.KML,
            ".kmz": SupportedFormat.KMZ,
        }
        if ext in ext_map:
            return ext_map[ext]

    raise UnsupportedFormatError(
        f"Unable to auto-detect geospatial format for payload of {len(data)} bytes"
        + (f" (filename: {filename})" if filename else "")
    )


def detect_format_from_file(file_path: Union[str, Path]) -> SupportedFormat:
    """Auto-detect geospatial format from a file path on disk."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found: {path}")

    # For directories containing shapefiles (.shp, .shx, .dbf)
    if path.is_dir():
        shp_files = list(path.glob("*.shp"))
        if shp_files:
            return SupportedFormat.SHAPEFILE
        raise UnsupportedFormatError(f"Directory {path} does not contain supported geospatial dataset files")

    # Read initial chunk for magic byte / header sniffing
    with open(path, "rb") as f:
        header_sample = f.read(65536)

    return detect_format_from_bytes(header_sample, filename=path.name)


class UnifiedIngestionDispatcher:
    """Dispatches dataset payloads to appropriate reader and returns normalized ingestion results."""

    @classmethod
    def ingest_bytes(
        cls,
        data: bytes,
        filename: Optional[str] = None,
        target_crs: Optional[str] = None,
        format_hint: Optional[SupportedFormat] = None,
    ) -> UnifiedIngestionResult:
        """Ingest raw byte buffer, auto-detecting format if not explicitly provided."""
        fmt = format_hint or detect_format_from_bytes(data, filename=filename)

        if fmt == SupportedFormat.GEOJSON:
            try:
                res_gj = GeoJSONIngestionReader.read_from_bytes(data, target_crs=target_crs)
            except (GeoJSONCorruptFileError, GeoJSONValidationError) as exc:
                raise CorruptDataError(f"GeoJSON ingestion failed: {exc}") from exc

            features = [
                UnifiedFeatureRecord(
                    id=f.feature_id,
                    geometry=f.geometry,
                    properties=f.properties,
                    crs=f.crs,
                    is_valid=f.is_valid,
                    bounds=f.bounds,
                    area=f.area,
                )
                for f in res_gj.features
            ]
            return UnifiedIngestionResult(
                format=SupportedFormat.GEOJSON,
                detected_crs=res_gj.detected_crs,
                feature_count=len(features),
                features=features,
                bounds=res_gj.bounds,
                is_raster=False,
                metadata={"valid_count": res_gj.valid_count, "invalid_count": res_gj.invalid_count},
            )

        elif fmt == SupportedFormat.SHAPEFILE:
            try:
                res_shp = ShapefileIngestionReader.read_from_zip(data, target_crs=target_crs)
            except (ShapefileCorruptArchiveError, ShapefileMissingComponentError, ShapefileValidationError) as exc:
                raise CorruptDataError(f"Shapefile ingestion failed: {exc}") from exc

            features = [
                UnifiedFeatureRecord(
                    id=f.feature_id,
                    geometry=f.geometry,
                    properties=f.properties,
                    crs=f.crs,
                    is_valid=f.is_valid,
                    bounds=f.bounds,
                    area=f.area,
                )
                for f in res_shp.features
            ]
            return UnifiedIngestionResult(
                format=SupportedFormat.SHAPEFILE,
                detected_crs=res_shp.detected_crs,
                feature_count=len(features),
                features=features,
                bounds=res_shp.bounds,
                is_raster=False,
                metadata={"valid_count": res_shp.valid_count, "invalid_count": res_shp.invalid_count},
            )

        elif fmt == SupportedFormat.DXF:
            try:
                res_dxf = DXFIngestionReader.read_from_bytes(data)
            except (DXFCorruptFileError, DXFEmptyFileError) as exc:
                raise CorruptDataError(f"DXF ingestion failed: {exc}") from exc

            features = []
            for e in res_dxf.entities:
                geom = e.geometry
                is_valid = geom.is_valid if geom else False
                b = geom.bounds if geom else (0.0, 0.0, 0.0, 0.0)
                area = geom.area if (geom and hasattr(geom, "area")) else None
                props = dict(e.properties)
                props["layer"] = e.layer
                props["entity_type"] = e.entity_type
                features.append(
                    UnifiedFeatureRecord(
                        id=e.handle,
                        geometry=geom,
                        properties=props,
                        crs=target_crs or "LOCAL",
                        is_valid=is_valid,
                        bounds=b,
                        area=area,
                    )
                )

            valid_count = sum(1 for f in features if f.is_valid)
            invalid_count = len(features) - valid_count
            return UnifiedIngestionResult(
                format=SupportedFormat.DXF,
                detected_crs=target_crs or "LOCAL",
                feature_count=len(features),
                features=features,
                bounds=res_dxf.bounds,
                is_raster=False,
                metadata={
                    "valid_count": valid_count,
                    "invalid_count": invalid_count,
                    "layers": res_dxf.layers,
                    "entity_counts": res_dxf.entity_counts,
                },
            )

        elif fmt == SupportedFormat.FLATGEOBUF:
            try:
                res_fgb = FlatGeobufIngestionReader.read_from_bytes(data, target_crs=target_crs)
            except (FlatGeobufInvalidMagicError, FlatGeobufCorruptHeaderError) as exc:
                raise CorruptDataError(f"FlatGeobuf ingestion failed: {exc}") from exc

            features = [
                UnifiedFeatureRecord(
                    id=f.feature_id,
                    geometry=f.geometry,
                    properties=f.properties,
                    crs=f.crs,
                    is_valid=f.is_valid,
                    bounds=f.bounds,
                    area=f.area,
                )
                for f in res_fgb.features
            ]
            feature_count = len(features) if features else res_fgb.total_count
            return UnifiedIngestionResult(
                format=SupportedFormat.FLATGEOBUF,
                detected_crs=res_fgb.detected_crs,
                feature_count=feature_count,
                features=features,
                bounds=res_fgb.bounds,
                is_raster=False,
                metadata={
                    "valid_count": res_fgb.valid_count,
                    "invalid_count": res_fgb.invalid_count,
                    "has_spatial_index": res_fgb.has_spatial_index,
                },
            )

        elif fmt == SupportedFormat.GEOTIFF:
            try:
                res_gt = GeoTIFFIngestionReader.read_from_bytes(data, target_crs=target_crs)
            except (GeoTIFFInvalidHeaderError, GeoTIFFCorruptIFDError, GeoTIFFMissingGeoreferenceError) as exc:
                raise CorruptDataError(f"GeoTIFF ingestion failed: {exc}") from exc

            return UnifiedIngestionResult(
                format=SupportedFormat.GEOTIFF,
                detected_crs=res_gt.detected_crs,
                feature_count=1,
                features=[],
                bounds=res_gt.bounds,
                is_raster=True,
                raster_metadata=res_gt.metadata,
                warnings=res_gt.warnings,
                metadata=res_gt.metadata.to_dict(),
            )

        raise UnsupportedFormatError(f"Ingestion reader for format {fmt} is not implemented")

    @classmethod
    def ingest_file(
        cls,
        file_path: Union[str, Path],
        target_crs: Optional[str] = None,
        format_hint: Optional[SupportedFormat] = None,
    ) -> UnifiedIngestionResult:
        """Ingest dataset from file on disk, auto-detecting format if not explicitly provided."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        fmt = format_hint or detect_format_from_file(path)

        if fmt == SupportedFormat.GEOTIFF:
            res_gt = GeoTIFFIngestionReader.read_from_file(path, target_crs=target_crs)
            return UnifiedIngestionResult(
                format=SupportedFormat.GEOTIFF,
                detected_crs=res_gt.detected_crs,
                feature_count=1,
                features=[],
                bounds=res_gt.bounds,
                is_raster=True,
                raster_metadata=res_gt.metadata,
                warnings=res_gt.warnings,
                metadata=res_gt.metadata.to_dict(),
            )

        # For files that can be read via their reader
        if fmt == SupportedFormat.SHAPEFILE and path.is_dir():
            res_shp = ShapefileIngestionReader.read_from_directory(path, target_crs=target_crs)
            features = [
                UnifiedFeatureRecord(
                    id=f.feature_id,
                    geometry=f.geometry,
                    properties=f.properties,
                    crs=f.crs,
                    is_valid=f.is_valid,
                    bounds=f.bounds,
                    area=f.area,
                )
                for f in res_shp.features
            ]
            return UnifiedIngestionResult(
                format=SupportedFormat.SHAPEFILE,
                detected_crs=res_shp.detected_crs,
                feature_count=len(features),
                features=features,
                bounds=res_shp.bounds,
                is_raster=False,
                metadata={"valid_count": res_shp.valid_count, "invalid_count": res_shp.invalid_count},
            )

        raw_bytes = path.read_bytes()
        return cls.ingest_bytes(raw_bytes, filename=path.name, target_crs=target_crs, format_hint=fmt)
