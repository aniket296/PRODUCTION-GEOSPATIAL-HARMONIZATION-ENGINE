"""GeoJSON Ingestion Reader with RFC 7946 validation, CRS extraction, and coordinate precision rounding."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.polygon import orient
from shapely.ops import transform


class GeoJSONExtractionError(Exception):
    """Base exception for GeoJSON ingestion errors."""
    pass


class GeoJSONCorruptFileError(GeoJSONExtractionError):
    """Raised when JSON payload is unparseable or corrupt."""
    pass


class GeoJSONValidationError(GeoJSONExtractionError):
    """Raised when GeoJSON structure violates RFC 7946 specification."""
    pass


class GeoJSONEmptyFileError(GeoJSONExtractionError):
    """Raised when GeoJSON document contains zero features."""
    pass


@dataclass
class GeoJSONFeatureRecord:
    """Standardized record extracted from GeoJSON feature."""
    feature_id: Optional[str]
    geometry: Any  # Shapely geometry (Polygon, MultiPolygon, etc.)
    properties: Dict[str, Any]
    crs: str
    is_valid: bool
    bounds: Tuple[float, float, float, float]
    area: Optional[float] = None


@dataclass
class GeoJSONIngestionResult:
    """Aggregated output of GeoJSON ingestion."""
    features: List[GeoJSONFeatureRecord]
    detected_crs: str
    total_count: int
    valid_count: int
    invalid_count: int
    bounds: Optional[Tuple[float, float, float, float]]


def extract_crs_from_geojson(geojson_dict: Dict[str, Any]) -> str:
    """Extract and standardize CRS identifier from GeoJSON object.

    Defaults to 'EPSG:4326' per RFC 7946 if no foreign CRS member is defined.
    Handles legacy GeoJSON CRS specifications:
    - urn:ogc:def:crs:OGC:1.3:CRS84 -> EPSG:4326
    - urn:ogc:def:crs:EPSG::24378   -> EPSG:24378
    - EPSG:32643                    -> EPSG:32643
    """
    crs_member = geojson_dict.get("crs")
    if not crs_member:
        return "EPSG:4326"

    props = crs_member.get("properties", {})
    name = props.get("name", "")

    if not name:
        return "EPSG:4326"

    # Match OGC CRS84 / WGS84
    if "CRS84" in name or "4326" in name:
        return "EPSG:4326"

    # Match urn:ogc:def:crs:EPSG::XXXXX or EPSG:XXXXX
    epsg_match = re.search(r"EPSG[:\s]*:?(\d+)", name, re.IGNORECASE)
    if epsg_match:
        return f"EPSG:{epsg_match.group(1)}"

    return name.strip()


def round_coordinates(geom: Any, precision: int = 7) -> Any:
    """Round all coordinates of a Shapely geometry to specified decimal precision.

    Drops the Z coordinate if present, ensuring 2D planar/geodetic representation.
    """
    def _round_coords(x: float, y: float, z: Optional[float] = None) -> Tuple[float, float]:
        return round(float(x), precision), round(float(y), precision)

    return transform(_round_coords, geom)


def enforce_rfc7946_winding_order(geom: Any) -> Any:
    """Enforce RFC 7946 Right-Hand Rule: exterior ring CCW (+1.0), holes CW (-1.0)."""
    if isinstance(geom, Polygon):
        return orient(geom, sign=1.0)
    elif isinstance(geom, MultiPolygon):
        oriented_parts = [orient(poly, sign=1.0) for poly in geom.geoms]
        return MultiPolygon(oriented_parts)
    return geom


class GeoJSONIngestionReader:
    """Reader for parsing, validating, and ingesting RFC 7946 GeoJSON documents."""

    @classmethod
    def read_from_dict(
        cls,
        data: Dict[str, Any],
        target_crs: Optional[str] = None,
        precision: Optional[int] = None,
        enforce_winding: bool = True,
    ) -> GeoJSONIngestionResult:
        """Parse GeoJSON from an in-memory dictionary."""
        if not isinstance(data, dict):
            raise GeoJSONValidationError(f"Expected GeoJSON root dict, got {type(data).__name__}")

        geo_type = data.get("type")
        if not geo_type:
            raise GeoJSONValidationError("Missing required 'type' field in GeoJSON root")

        detected_crs = target_crs or extract_crs_from_geojson(data)

        # Default coordinate precision: 7 decimals for EPSG:4326, 3 decimals for metric grids
        if precision is None:
            precision = 7 if detected_crs == "EPSG:4326" else 3

        # Normalize features list
        if geo_type == "FeatureCollection":
            raw_features = data.get("features")
            if raw_features is None:
                raise GeoJSONValidationError("FeatureCollection missing 'features' array")
            if len(raw_features) == 0:
                raise GeoJSONEmptyFileError("FeatureCollection contains 0 features")
        elif geo_type == "Feature":
            raw_features = [data]
        elif geo_type in ("Polygon", "MultiPolygon", "Point", "LineString", "MultiPoint", "MultiLineString"):
            # Naked geometry root: wrap into single synthetic feature
            raw_features = [{"type": "Feature", "geometry": data, "properties": {}}]
        else:
            raise GeoJSONValidationError(f"Unsupported root GeoJSON type: '{geo_type}'")

        records: List[GeoJSONFeatureRecord] = []
        valid_count = 0
        invalid_count = 0

        for idx, feat in enumerate(raw_features):
            if not isinstance(feat, dict) or feat.get("type") != "Feature":
                raise GeoJSONValidationError(f"Invalid Feature at index {idx}")

            geom_dict = feat.get("geometry")
            if not geom_dict:
                continue

            try:
                shapely_geom = shape(geom_dict)
            except Exception as exc:
                raise GeoJSONValidationError(f"Failed to parse geometry at index {idx}: {exc}") from exc

            # Round coordinates and drop Z
            shapely_geom = round_coordinates(shapely_geom, precision=precision)

            # Orient polygon rings per RFC 7946
            if enforce_winding:
                shapely_geom = enforce_rfc7946_winding_order(shapely_geom)

            is_valid = bool(shapely_geom.is_valid)
            if is_valid:
                valid_count += 1
            else:
                invalid_count += 1

            feature_id = str(feat.get("id")) if feat.get("id") is not None else None
            properties = feat.get("properties") or {}

            area_val = float(shapely_geom.area) if hasattr(shapely_geom, "area") else None

            record = GeoJSONFeatureRecord(
                feature_id=feature_id,
                geometry=shapely_geom,
                properties=properties,
                crs=detected_crs,
                is_valid=is_valid,
                bounds=shapely_geom.bounds,
                area=area_val,
            )
            records.append(record)

        if not records:
            raise GeoJSONEmptyFileError("No valid geometries found in GeoJSON document")

        # Global bounding box
        all_minx = min(r.bounds[0] for r in records)
        all_miny = min(r.bounds[1] for r in records)
        all_maxx = max(r.bounds[2] for r in records)
        all_maxy = max(r.bounds[3] for r in records)
        global_bounds = (all_minx, all_miny, all_maxx, all_maxy)

        return GeoJSONIngestionResult(
            features=records,
            detected_crs=detected_crs,
            total_count=len(records),
            valid_count=valid_count,
            invalid_count=invalid_count,
            bounds=global_bounds,
        )

    @classmethod
    def read_from_text(
        cls,
        text: str,
        target_crs: Optional[str] = None,
        precision: Optional[int] = None,
        enforce_winding: bool = True,
    ) -> GeoJSONIngestionResult:
        """Parse GeoJSON from a JSON-encoded string."""
        if not text or not text.strip():
            raise GeoJSONCorruptFileError("GeoJSON content is empty")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeoJSONCorruptFileError(f"Malformed JSON document: {exc}") from exc
        return cls.read_from_dict(
            data,
            target_crs=target_crs,
            precision=precision,
            enforce_winding=enforce_winding,
        )

    @classmethod
    def read_from_bytes(
        cls,
        raw_bytes: bytes,
        target_crs: Optional[str] = None,
        precision: Optional[int] = None,
        enforce_winding: bool = True,
    ) -> GeoJSONIngestionResult:
        """Parse GeoJSON from byte stream."""
        if not raw_bytes:
            raise GeoJSONCorruptFileError("GeoJSON byte stream is empty")
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw_bytes.decode("latin-1")
            except Exception as exc:
                raise GeoJSONCorruptFileError(f"Failed to decode GeoJSON bytes: {exc}") from exc
        return cls.read_from_text(
            text,
            target_crs=target_crs,
            precision=precision,
            enforce_winding=enforce_winding,
        )

    @classmethod
    def read_from_file(
        cls,
        file_path: Union[str, Path],
        target_crs: Optional[str] = None,
        precision: Optional[int] = None,
        enforce_winding: bool = True,
    ) -> GeoJSONIngestionResult:
        """Parse GeoJSON from a file on disk."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"GeoJSON file not found: {path}")
        raw_bytes = path.read_bytes()
        return cls.read_from_bytes(
            raw_bytes,
            target_crs=target_crs,
            precision=precision,
            enforce_winding=enforce_winding,
        )
