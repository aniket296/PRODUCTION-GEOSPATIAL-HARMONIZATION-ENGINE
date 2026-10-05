"""Unified ingestion pipeline: format auto-detection and reader dispatching."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import struct
from typing import Any, Dict, List, Optional, Tuple, Union
import zipfile

from shapely.geometry import Polygon, mapping

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
class FormatDetectionResult:
    """Detailed result of automated geospatial format detection."""

    format: SupportedFormat
    confidence: float
    mime_type: str
    detection_method: str
    details: Dict[str, Any] = field(default_factory=dict)


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

    def to_dict(self) -> Dict[str, Any]:
        """Convert feature record to dictionary representation."""
        return {
            "id": self.id,
            "crs": self.crs,
            "is_valid": self.is_valid,
            "bounds": list(self.bounds),
            "area": self.area,
            "properties": dict(self.properties),
            "geometry_type": getattr(self.geometry, "geom_type", None) if self.geometry else None,
        }

    def to_geojson_feature(self) -> Dict[str, Any]:
        """Export record as standard GeoJSON feature."""
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": mapping(self.geometry) if self.geometry else None,
            "properties": dict(self.properties),
        }


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

    def to_dict(self) -> Dict[str, Any]:
        """Serialize ingestion result summary."""
        return {
            "format": self.format.value,
            "detected_crs": self.detected_crs,
            "feature_count": self.feature_count,
            "bounds": list(self.bounds) if self.bounds else None,
            "is_raster": self.is_raster,
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }

    def to_geojson_feature_collection(self) -> Dict[str, Any]:
        """Export vector features as standard GeoJSON FeatureCollection."""
        if self.is_raster:
            raise ValueError("Raster datasets cannot be exported directly as GeoJSON FeatureCollection")
        return {
            "type": "FeatureCollection",
            "crs": {
                "type": "name",
                "properties": {
                    "name": (
                        f"urn:ogc:def:crs:EPSG::{self.detected_crs.split(':')[-1]}"
                        if self.detected_crs and ":" in self.detected_crs
                        else (self.detected_crs or "EPSG:4326")
                    )
                },
            },
            "features": [f.to_geojson_feature() for f in self.features],
            "bbox": list(self.bounds) if self.bounds else None,
        }


class GeospatialFormatDetector:
    """Intelligent multi-tier format detector combining magic bytes, archive inspection, and content parsing."""

    MIME_TYPES: Dict[SupportedFormat, str] = {
        SupportedFormat.GEOJSON: "application/geo+json",
        SupportedFormat.SHAPEFILE: "application/x-shapefile",
        SupportedFormat.DXF: "image/vnd.dxf",
        SupportedFormat.FLATGEOBUF: "application/vnd.flatgeobuf",
        SupportedFormat.GEOTIFF: "image/tiff",
        SupportedFormat.KML: "application/vnd.google-earth.kml+xml",
        SupportedFormat.KMZ: "application/vnd.google-earth.kmz",
    }

    EXTENSION_MAP: Dict[str, SupportedFormat] = {
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

    @classmethod
    def detect_bytes(
        cls,
        data: bytes,
        filename: Optional[str] = None,
    ) -> FormatDetectionResult:
        """Auto-detect geospatial format from binary contents and optional filename hint."""
        if not data or len(data) == 0:
            raise CorruptDataError("Cannot detect format: payload contains 0 bytes")

        # 1. FlatGeobuf magic bytes: b'fgb\x03fgb\x00'
        if len(data) >= 8 and data[:3] == b"fgb" and data[3] == 3 and data[4:7] == b"fgb":
            return FormatDetectionResult(
                format=SupportedFormat.FLATGEOBUF,
                confidence=1.0,
                mime_type=cls.MIME_TYPES[SupportedFormat.FLATGEOBUF],
                detection_method="MAGIC_BYTES",
                details={"magic": "fgb\\x03fgb\\x00"},
            )

        # 2. GeoTIFF / TIFF magic bytes
        if len(data) >= 4:
            # Classic TIFF: II*\x00 (LE) or MM\x00* (BE)
            # BigTIFF: II+\x00 (LE) or MM\x00+ (BE)
            if (
                data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
                or (data[:2] in (b"II", b"MM") and struct.unpack("<H" if data[:2] == b"II" else ">H", data[2:4])[0] in (42, 43))
            ):
                return FormatDetectionResult(
                    format=SupportedFormat.GEOTIFF,
                    confidence=1.0,
                    mime_type=cls.MIME_TYPES[SupportedFormat.GEOTIFF],
                    detection_method="MAGIC_BYTES",
                    details={"header_bytes": data[:4].hex()},
                )

        # 3. ZIP archive: PK\x03\x04 (Shapefile ZIP or KMZ)
        if len(data) >= 4 and data[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    namelist = [name.lower() for name in zf.namelist()]
                    if any(n.endswith(".shp") for n in namelist):
                        return FormatDetectionResult(
                            format=SupportedFormat.SHAPEFILE,
                            confidence=0.98,
                            mime_type="application/zip",
                            detection_method="ARCHIVE_INSPECTION",
                            details={"archive_files": namelist[:10]},
                        )
                    if any(n.endswith(".kml") for n in namelist):
                        return FormatDetectionResult(
                            format=SupportedFormat.KMZ,
                            confidence=0.98,
                            mime_type=cls.MIME_TYPES[SupportedFormat.KMZ],
                            detection_method="ARCHIVE_INSPECTION",
                            details={"archive_files": namelist[:10]},
                        )
            except Exception:
                pass
            # Fallback to extension if valid zip but non-standard structure
            if filename:
                fn_lower = filename.lower()
                if fn_lower.endswith(".zip"):
                    return FormatDetectionResult(
                        format=SupportedFormat.SHAPEFILE,
                        confidence=0.75,
                        mime_type="application/zip",
                        detection_method="FILENAME_EXTENSION",
                        details={"filename": filename},
                    )
                if fn_lower.endswith(".kmz"):
                    return FormatDetectionResult(
                        format=SupportedFormat.KMZ,
                        confidence=0.75,
                        mime_type=cls.MIME_TYPES[SupportedFormat.KMZ],
                        detection_method="FILENAME_EXTENSION",
                        details={"filename": filename},
                    )

        # 4. Standalone Shapefile binary (.shp file code 9994 = 0x0000270a)
        if len(data) >= 4 and data[:4] == b"\x00\x00\x27\x0a":
            return FormatDetectionResult(
                format=SupportedFormat.SHAPEFILE,
                confidence=1.0,
                mime_type=cls.MIME_TYPES[SupportedFormat.SHAPEFILE],
                detection_method="MAGIC_BYTES",
                details={"file_code": 9994},
            )

        # 5. AutoCAD Binary DXF: starts with "AutoCAD Binary DXF\r\n\x1a\x00"
        if len(data) >= 22 and data.startswith(b"AutoCAD Binary DXF"):
            return FormatDetectionResult(
                format=SupportedFormat.DXF,
                confidence=1.0,
                mime_type=cls.MIME_TYPES[SupportedFormat.DXF],
                detection_method="MAGIC_BYTES",
                details={"header": "AutoCAD Binary DXF"},
            )

        # 6. Text-based detection: DXF ASCII, GeoJSON, KML
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
                return FormatDetectionResult(
                    format=SupportedFormat.DXF,
                    confidence=0.95,
                    mime_type=cls.MIME_TYPES[SupportedFormat.DXF],
                    detection_method="TEXT_STRUCTURE",
                    details={"dxf_type": "ASCII"},
                )

            # KML XML
            if "<kml" in stripped.lower() or ("<?xml" in stripped and "<document" in stripped.lower()):
                return FormatDetectionResult(
                    format=SupportedFormat.KML,
                    confidence=0.95,
                    mime_type=cls.MIME_TYPES[SupportedFormat.KML],
                    detection_method="TEXT_STRUCTURE",
                    details={"xml_type": "kml"},
                )

            # GeoJSON: starts with '{' or '[' and contains GeoJSON indicators
            if stripped.startswith("{") or stripped.startswith("["):
                try:
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
                            return FormatDetectionResult(
                                format=SupportedFormat.GEOJSON,
                                confidence=0.98,
                                mime_type=cls.MIME_TYPES[SupportedFormat.GEOJSON],
                                detection_method="TEXT_STRUCTURE",
                                details={"json_type": parsed["type"]},
                            )
                except Exception:
                    if '"FeatureCollection"' in text_sample or '"features"' in text_sample or '"geometry"' in text_sample:
                        return FormatDetectionResult(
                            format=SupportedFormat.GEOJSON,
                            confidence=0.90,
                            mime_type=cls.MIME_TYPES[SupportedFormat.GEOJSON],
                            detection_method="TEXT_STRUCTURE",
                            details={"heuristic": "geojson_keywords"},
                        )

        # 7. Fallback to filename extension
        if filename:
            ext = Path(filename).suffix.lower()
            if ext in cls.EXTENSION_MAP:
                fmt = cls.EXTENSION_MAP[ext]
                return FormatDetectionResult(
                    format=fmt,
                    confidence=0.70,
                    mime_type=cls.MIME_TYPES.get(fmt, "application/octet-stream"),
                    detection_method="FILENAME_EXTENSION",
                    details={"extension": ext, "filename": filename},
                )

        raise UnsupportedFormatError(
            f"Unable to auto-detect geospatial format for payload of {len(data)} bytes"
            + (f" (filename: {filename})" if filename else "")
        )

    @classmethod
    def detect_file(cls, file_path: Union[str, Path]) -> FormatDetectionResult:
        """Auto-detect geospatial format from a file path on disk."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Dataset file not found: {path}")

        # For directories containing shapefiles (.shp, .shx, .dbf)
        if path.is_dir():
            shp_files = list(path.glob("*.shp"))
            if shp_files:
                return FormatDetectionResult(
                    format=SupportedFormat.SHAPEFILE,
                    confidence=0.95,
                    mime_type="application/x-directory",
                    detection_method="DIRECTORY_INSPECTION",
                    details={"shp_files": [f.name for f in shp_files]},
                )
            raise UnsupportedFormatError(f"Directory {path} does not contain supported geospatial dataset files")

        # Read initial chunk for magic byte / header sniffing
        with open(path, "rb") as f:
            header_sample = f.read(65536)

        return cls.detect_bytes(header_sample, filename=path.name)


def detect_format_from_bytes(
    data: bytes,
    filename: Optional[str] = None,
) -> SupportedFormat:
    """Auto-detect geospatial format from binary contents and optional filename hint."""
    return GeospatialFormatDetector.detect_bytes(data, filename=filename).format


def detect_format_from_file(file_path: Union[str, Path]) -> SupportedFormat:
    """Auto-detect geospatial format from a file path on disk."""
    return GeospatialFormatDetector.detect_file(file_path).format


def _compute_geometry_stats(features: List[UnifiedFeatureRecord]) -> Tuple[Dict[str, int], float]:
    """Helper calculating geometry type frequencies and total area across features."""
    geom_types: Dict[str, int] = {}
    total_area = 0.0
    for feat in features:
        if feat.geometry:
            gtype = getattr(feat.geometry, "geom_type", "Unknown")
            geom_types[gtype] = geom_types.get(gtype, 0) + 1
        if feat.area is not None:
            total_area += feat.area
    return geom_types, total_area


class UnifiedIngestionDispatcher:
    """Dispatches dataset payloads to appropriate reader and returns normalized ingestion results."""

    @classmethod
    def detect(cls, data: bytes, filename: Optional[str] = None) -> FormatDetectionResult:
        """Auto-detect geospatial format with confidence score and metadata."""
        return GeospatialFormatDetector.detect_bytes(data, filename=filename)

    @classmethod
    def detect_file(cls, file_path: Union[str, Path]) -> FormatDetectionResult:
        """Auto-detect format from file on disk with confidence score and metadata."""
        return GeospatialFormatDetector.detect_file(file_path)

    @classmethod
    def ingest_bytes(
        cls,
        data: bytes,
        filename: Optional[str] = None,
        target_crs: Optional[str] = None,
        format_hint: Optional[SupportedFormat] = None,
        max_features: Optional[int] = None,
        skip_invalid: bool = False,
    ) -> UnifiedIngestionResult:
        """Ingest raw byte buffer, auto-detecting format if not explicitly provided."""
        detection = None
        if not format_hint:
            detection = GeospatialFormatDetector.detect_bytes(data, filename=filename)
            fmt = detection.format
        else:
            fmt = format_hint

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
            if skip_invalid:
                features = [f for f in features if f.is_valid]
            if max_features is not None:
                features = features[:max_features]

            geom_types, total_area = _compute_geometry_stats(features)
            meta: Dict[str, Any] = {
                "valid_count": res_gj.valid_count,
                "invalid_count": res_gj.invalid_count,
                "geometry_types": geom_types,
                "total_area_sqm": total_area,
            }
            if detection:
                meta["detection_confidence"] = detection.confidence
                meta["mime_type"] = detection.mime_type

            return UnifiedIngestionResult(
                format=SupportedFormat.GEOJSON,
                detected_crs=res_gj.detected_crs,
                feature_count=len(features),
                features=features,
                bounds=res_gj.bounds,
                is_raster=False,
                metadata=meta,
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
            if skip_invalid:
                features = [f for f in features if f.is_valid]
            if max_features is not None:
                features = features[:max_features]

            geom_types, total_area = _compute_geometry_stats(features)
            meta = {
                "valid_count": res_shp.valid_count,
                "invalid_count": res_shp.invalid_count,
                "geometry_types": geom_types,
                "total_area_sqm": total_area,
            }
            if detection:
                meta["detection_confidence"] = detection.confidence
                meta["mime_type"] = detection.mime_type

            return UnifiedIngestionResult(
                format=SupportedFormat.SHAPEFILE,
                detected_crs=res_shp.detected_crs,
                feature_count=len(features),
                features=features,
                bounds=res_shp.bounds,
                is_raster=False,
                metadata=meta,
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

            if skip_invalid:
                features = [f for f in features if f.is_valid]
            if max_features is not None:
                features = features[:max_features]

            valid_count = sum(1 for f in features if f.is_valid)
            invalid_count = len(features) - valid_count
            geom_types, total_area = _compute_geometry_stats(features)
            meta = {
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "layers": res_dxf.layers,
                "entity_counts": res_dxf.entity_counts,
                "geometry_types": geom_types,
                "total_area_sqm": total_area,
            }
            if detection:
                meta["detection_confidence"] = detection.confidence
                meta["mime_type"] = detection.mime_type

            return UnifiedIngestionResult(
                format=SupportedFormat.DXF,
                detected_crs=target_crs or "LOCAL",
                feature_count=len(features),
                features=features,
                bounds=res_dxf.bounds,
                is_raster=False,
                metadata=meta,
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
            if skip_invalid:
                features = [f for f in features if f.is_valid]
            if max_features is not None:
                features = features[:max_features]

            feature_count = len(features) if features else res_fgb.total_count
            geom_types, total_area = _compute_geometry_stats(features)
            meta = {
                "valid_count": res_fgb.valid_count,
                "invalid_count": res_fgb.invalid_count,
                "has_spatial_index": res_fgb.has_spatial_index,
                "geometry_types": geom_types,
                "total_area_sqm": total_area,
            }
            if detection:
                meta["detection_confidence"] = detection.confidence
                meta["mime_type"] = detection.mime_type

            return UnifiedIngestionResult(
                format=SupportedFormat.FLATGEOBUF,
                detected_crs=res_fgb.detected_crs,
                feature_count=feature_count,
                features=features,
                bounds=res_fgb.bounds,
                is_raster=False,
                metadata=meta,
            )

        elif fmt == SupportedFormat.GEOTIFF:
            try:
                res_gt = GeoTIFFIngestionReader.read_from_bytes(data, target_crs=target_crs)
            except (GeoTIFFInvalidHeaderError, GeoTIFFCorruptIFDError, GeoTIFFMissingGeoreferenceError) as exc:
                raise CorruptDataError(f"GeoTIFF ingestion failed: {exc}") from exc

            meta = res_gt.metadata.to_dict()
            if detection:
                meta["detection_confidence"] = detection.confidence
                meta["mime_type"] = detection.mime_type

            return UnifiedIngestionResult(
                format=SupportedFormat.GEOTIFF,
                detected_crs=res_gt.detected_crs,
                feature_count=1,
                features=[],
                bounds=res_gt.bounds,
                is_raster=True,
                raster_metadata=res_gt.metadata,
                warnings=res_gt.warnings,
                metadata=meta,
            )

        raise UnsupportedFormatError(f"Ingestion reader for format {fmt} is not implemented")

    @classmethod
    def ingest_file(
        cls,
        file_path: Union[str, Path],
        target_crs: Optional[str] = None,
        format_hint: Optional[SupportedFormat] = None,
        max_features: Optional[int] = None,
        skip_invalid: bool = False,
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
            if skip_invalid:
                features = [f for f in features if f.is_valid]
            if max_features is not None:
                features = features[:max_features]

            geom_types, total_area = _compute_geometry_stats(features)
            return UnifiedIngestionResult(
                format=SupportedFormat.SHAPEFILE,
                detected_crs=res_shp.detected_crs,
                feature_count=len(features),
                features=features,
                bounds=res_shp.bounds,
                is_raster=False,
                metadata={
                    "valid_count": res_shp.valid_count,
                    "invalid_count": res_shp.invalid_count,
                    "geometry_types": geom_types,
                    "total_area_sqm": total_area,
                },
            )

        raw_bytes = path.read_bytes()
        return cls.ingest_bytes(
            raw_bytes,
            filename=path.name,
            target_crs=target_crs,
            format_hint=fmt,
            max_features=max_features,
            skip_invalid=skip_invalid,
        )
