"""Shapefile Ingestion Reader with component validation (.shp, .shx, .dbf, .prj) and ZIP unpacking."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import zipfile

import pyproj
from shapely.geometry import MultiPolygon, Polygon, shape
import shapefile  # pyshp

from backend.ingestion.geojson_reader import (
    enforce_rfc7946_winding_order,
    normalize_cadastral_properties,
    remove_duplicate_consecutive_vertices,
    round_coordinates,
)


class ShapefileExtractionError(Exception):
    """Base exception for Shapefile ingestion errors."""
    pass


class ShapefileMissingComponentError(ShapefileExtractionError):
    """Raised when mandatory Shapefile component files (.shp, .shx, .dbf) are missing."""
    pass


class ShapefileCorruptArchiveError(ShapefileExtractionError):
    """Raised when Shapefile ZIP archive is unparseable or corrupt."""
    pass


class ShapefileValidationError(ShapefileExtractionError):
    """Raised when Shapefile geometries or attribute structures are invalid."""
    pass


@dataclass
class ShapefileComponentSet:
    """Validated set of Shapefile component file paths."""
    base_name: str
    shp_path: Path
    shx_path: Path
    dbf_path: Path
    prj_path: Optional[Path] = None
    cpg_path: Optional[Path] = None

    def as_dict(self) -> Dict[str, str]:
        d = {
            "shp": str(self.shp_path),
            "shx": str(self.shx_path),
            "dbf": str(self.dbf_path),
        }
        if self.prj_path:
            d["prj"] = str(self.prj_path)
        if self.cpg_path:
            d["cpg"] = str(self.cpg_path)
        return d


@dataclass
class ShapefileFeatureRecord:
    """Standardized record extracted from Shapefile."""
    feature_id: Optional[str]
    geometry: Any  # Shapely Polygon / MultiPolygon
    properties: Dict[str, Any]
    crs: str
    is_valid: bool
    bounds: Tuple[float, float, float, float]
    area: Optional[float] = None


@dataclass
class ShapefileIngestionResult:
    """Aggregated output of Shapefile ingestion."""
    features: List[ShapefileFeatureRecord]
    detected_crs: str
    total_count: int
    valid_count: int
    invalid_count: int
    bounds: Optional[Tuple[float, float, float, float]]
    components: Dict[str, str] = field(default_factory=dict)


def parse_prj_to_crs(prj_text: str) -> str:
    """Parse ESRI/OGC WKT PRJ text into canonical EPSG CRS string (e.g. 'EPSG:4326')."""
    if not prj_text or not prj_text.strip():
        return "EPSG:4326"

    cleaned_prj = prj_text.strip()

    # 1. Try pyproj CRS from WKT
    try:
        crs = pyproj.CRS.from_wkt(cleaned_prj)
        epsg_code = crs.to_epsg()
        if epsg_code:
            return f"EPSG:{epsg_code}"
        # Try authority name + code
        auth_name = crs.to_authority()
        if auth_name:
            return f"{auth_name[0]}:{auth_name[1]}"
    except Exception:
        pass

    # 2. Heuristic fallback for common Indian revenue projection keywords
    upper_prj = cleaned_prj.upper()
    if "KALIANPUR" in upper_prj or "INDIA ZONE IIA" in upper_prj or "24378" in upper_prj:
        return "EPSG:24378"
    if "UTM ZONE 43N" in upper_prj or "UTM_ZONE_43N" in upper_prj or "32643" in upper_prj:
        return "EPSG:32643"
    if "WGS_1984" in upper_prj or "WGS 84" in upper_prj or "4326" in upper_prj:
        return "EPSG:4326"

    # Match AUTHORITY["EPSG","XXXXX"]
    match = re.search(r'AUTHORITY\["EPSG",\s*"(\d+)"\]', cleaned_prj, re.IGNORECASE)
    if match:
        return f"EPSG:{match.group(1)}"

    return "EPSG:4326"


def validate_shapefile_components(
    file_paths: List[Path],
    base_name: Optional[str] = None,
) -> ShapefileComponentSet:
    """Validate that mandatory components (.shp, .shx, .dbf) exist for a common base stem."""
    if not file_paths:
        raise ShapefileMissingComponentError("No files provided for Shapefile component validation")

    # Group files by stem (case-insensitive)
    stem_map: Dict[str, Dict[str, Path]] = {}
    for p in file_paths:
        stem = p.stem.lower()
        ext = p.suffix.lower()
        if stem not in stem_map:
            stem_map[stem] = {}
        stem_map[stem][ext] = p

    if base_name:
        target_stem = base_name.lower()
        if target_stem not in stem_map:
            raise ShapefileMissingComponentError(
                f"No Shapefile components found matching specified base name '{base_name}'"
            )
        chosen_stem = target_stem
    else:
        # Pick the first stem that contains .shp
        shp_stems = [s for s, exts in stem_map.items() if ".shp" in exts]
        if not shp_stems:
            raise ShapefileMissingComponentError("Missing mandatory geometry component: .shp")
        if len(shp_stems) > 1:
            raise ShapefileValidationError(
                f"Multiple Shapefile sets detected in directory: {shp_stems}. Specify base_name."
            )
        chosen_stem = shp_stems[0]

    components = stem_map[chosen_stem]

    # Check mandatory components
    if ".shp" not in components:
        raise ShapefileMissingComponentError(f"Missing mandatory geometry component: {chosen_stem}.shp")
    if ".shx" not in components:
        raise ShapefileMissingComponentError(f"Missing mandatory index component: {chosen_stem}.shx")
    if ".dbf" not in components:
        raise ShapefileMissingComponentError(f"Missing mandatory attribute table component: {chosen_stem}.dbf")

    return ShapefileComponentSet(
        base_name=chosen_stem,
        shp_path=components[".shp"],
        shx_path=components[".shx"],
        dbf_path=components[".dbf"],
        prj_path=components.get(".prj"),
        cpg_path=components.get(".cpg"),
    )


def unpack_shapefile_zip(
    zip_source: Union[bytes, Path, str],
    extract_dir: Path,
    base_name: Optional[str] = None,
) -> ShapefileComponentSet:
    """Extract Shapefile archive and validate its component files."""
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        if isinstance(zip_source, bytes):
            with zipfile.ZipFile(io.BytesIO(zip_source)) as zf:
                zf.extractall(extract_dir)
        else:
            with zipfile.ZipFile(Path(zip_source)) as zf:
                zf.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise ShapefileCorruptArchiveError(f"Invalid or corrupted ZIP archive: {exc}") from exc
    except Exception as exc:
        raise ShapefileCorruptArchiveError(f"Failed to unpack ZIP archive: {exc}") from exc

    # Recursively find all extracted files
    all_files = [p for p in extract_dir.rglob("*") if p.is_file()]
    return validate_shapefile_components(all_files, base_name=base_name)


class ShapefileIngestionReader:
    """Reader for parsing, validating, and ingesting Shapefile archives and component sets."""

    @classmethod
    def read_from_component_set(
        cls,
        component_set: ShapefileComponentSet,
        target_crs: Optional[str] = None,
        precision: Optional[int] = None,
        enforce_winding: bool = True,
        remove_duplicates: bool = True,
    ) -> ShapefileIngestionResult:
        """Parse features from a validated ShapefileComponentSet."""
        # Detect CRS from .prj if available
        if target_crs:
            detected_crs = target_crs
        elif component_set.prj_path and component_set.prj_path.exists():
            prj_text = component_set.prj_path.read_text(encoding="utf-8", errors="ignore")
            detected_crs = parse_prj_to_crs(prj_text)
        else:
            detected_crs = "EPSG:4326"

        if precision is None:
            precision = 7 if detected_crs == "EPSG:4326" else 3

        # Read using pyshp
        try:
            with shapefile.Reader(
                shp=str(component_set.shp_path),
                shx=str(component_set.shx_path),
                dbf=str(component_set.dbf_path),
            ) as reader:
                shape_records = reader.shapeRecords()
                if not shape_records:
                    raise ShapefileValidationError("Shapefile contains 0 feature records")
                fields = [f[0] for f in reader.fields[1:]]  # Exclude deletion flag field
        except ShapefileValidationError:
            raise
        except Exception as exc:
            raise ShapefileValidationError(f"Failed to open Shapefile with pyshp: {exc}") from exc

        records: List[ShapefileFeatureRecord] = []
        valid_count = 0
        invalid_count = 0

        for idx, sr in enumerate(shape_records):
            shp_geom = sr.shape
            # Convert pyshp shape to Shapely geometry via __geo_interface__
            try:
                raw_geom = shape(shp_geom.__geo_interface__)
            except Exception as exc:
                raise ShapefileValidationError(f"Invalid geometry at index {idx}: {exc}") from exc

            # Round coordinates
            cleaned_geom = round_coordinates(raw_geom, precision=precision)

            # Deduplicate consecutive vertices
            if remove_duplicates:
                cleaned_geom = remove_duplicate_consecutive_vertices(cleaned_geom)

            # Enforce RFC 7946 winding order
            if enforce_winding:
                cleaned_geom, _ = enforce_rfc7946_winding_order(cleaned_geom)

            is_valid = bool(cleaned_geom.is_valid)
            if is_valid:
                valid_count += 1
            else:
                invalid_count += 1

            # Extract properties from DBF record
            raw_props = {f: sr.record[i] for i, f in enumerate(fields)}
            props = normalize_cadastral_properties(raw_props)

            feature_id = (
                str(props.get("khasra_number") or props.get("parcel_id") or f"FEAT-{idx + 1}")
            )
            area_val = float(cleaned_geom.area) if hasattr(cleaned_geom, "area") else None

            feat_rec = ShapefileFeatureRecord(
                feature_id=feature_id,
                geometry=cleaned_geom,
                properties=props,
                crs=detected_crs,
                is_valid=is_valid,
                bounds=cleaned_geom.bounds,
                area=area_val,
            )
            records.append(feat_rec)

        # Global bounds
        all_minx = min(r.bounds[0] for r in records)
        all_miny = min(r.bounds[1] for r in records)
        all_maxx = max(r.bounds[2] for r in records)
        all_maxy = max(r.bounds[3] for r in records)
        global_bounds = (all_minx, all_miny, all_maxx, all_maxy)

        return ShapefileIngestionResult(
            features=records,
            detected_crs=detected_crs,
            total_count=len(records),
            valid_count=valid_count,
            invalid_count=invalid_count,
            bounds=global_bounds,
            components=component_set.as_dict(),
        )

    @classmethod
    def read_from_zip(
        cls,
        zip_source: Union[bytes, Path, str],
        target_crs: Optional[str] = None,
        base_name: Optional[str] = None,
        precision: Optional[int] = None,
        enforce_winding: bool = True,
        remove_duplicates: bool = True,
    ) -> ShapefileIngestionResult:
        """Extract and parse Shapefile from a ZIP archive or bytes stream."""
        with tempfile.TemporaryDirectory(prefix="shp_ingest_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            comp_set = unpack_shapefile_zip(zip_source, extract_dir=tmp_path, base_name=base_name)
            return cls.read_from_component_set(
                comp_set,
                target_crs=target_crs,
                precision=precision,
                enforce_winding=enforce_winding,
                remove_duplicates=remove_duplicates,
            )

    @classmethod
    def read_from_directory(
        cls,
        directory: Union[str, Path],
        target_crs: Optional[str] = None,
        base_name: Optional[str] = None,
        precision: Optional[int] = None,
        enforce_winding: bool = True,
        remove_duplicates: bool = True,
    ) -> ShapefileIngestionResult:
        """Parse Shapefile from a filesystem directory."""
        dir_path = Path(directory)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"Directory not found: {dir_path}")
        all_files = [p for p in dir_path.glob("*") if p.is_file()]
        comp_set = validate_shapefile_components(all_files, base_name=base_name)
        return cls.read_from_component_set(
            comp_set,
            target_crs=target_crs,
            precision=precision,
            enforce_winding=enforce_winding,
            remove_duplicates=remove_duplicates,
        )
