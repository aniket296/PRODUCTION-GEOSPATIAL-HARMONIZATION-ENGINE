"""DXF Ingestion Reader using ezdxf for cadastral entity extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import ezdxf
from ezdxf.document import Drawing
from ezdxf.lldxf.const import DXFStructureError, DXFValueError
from shapely.geometry import LineString, Point, Polygon


class DXFExtractionError(Exception):
    """Base exception for DXF extraction errors."""
    pass


class DXFCorruptFileError(DXFExtractionError):
    """Raised when DXF file structure is corrupt or unparseable."""
    pass


class DXFEmptyFileError(DXFExtractionError):
    """Raised when DXF document contains zero extractable entities."""
    pass


@dataclass
class DXFEntityRecord:
    """Standardized extracted entity from DXF drawing."""
    entity_type: str
    layer: str
    geometry: Any  # Shapely geometry (Polygon, LineString, Point)
    handle: str
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DXFIngestionResult:
    """Comprehensive output of DXF ingestion process."""
    entities: List[DXFEntityRecord]
    layers: List[str]
    bounds: Optional[Tuple[float, float, float, float]]
    entity_counts: Dict[str, int]
    layer_counts: Dict[str, int] = field(default_factory=dict)
    invalid_geometries_count: int = 0


class DXFIngestionReader:
    """Reader for extracting cadastral boundaries and spatial markers from DXF drawings."""

    @staticmethod
    def _load_drawing_from_text(dxf_text: str) -> Drawing:
        """Parse Drawing object from ASCII DXF string with robust CRLF normalization."""
        if not dxf_text or not dxf_text.strip():
            raise DXFCorruptFileError("DXF content is empty")
        normalized_text = dxf_text.replace("\r\n", "\n").replace("\r", "\n")
        try:
            return ezdxf.read(StringIO(normalized_text))
        except (DXFStructureError, DXFValueError, Exception) as exc:
            raise DXFCorruptFileError(f"Failed to parse DXF structure: {exc}") from exc

    @staticmethod
    def _load_drawing_from_bytes(dxf_bytes: bytes) -> Drawing:
        """Parse Drawing object from binary bytes stream."""
        if not dxf_bytes:
            raise DXFCorruptFileError("DXF byte stream is empty")
        try:
            text = dxf_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = dxf_bytes.decode("latin-1")
            except Exception as exc:
                raise DXFCorruptFileError(f"Failed to decode DXF bytes: {exc}") from exc
        return DXFIngestionReader._load_drawing_from_text(text)

    @classmethod
    def list_layers(cls, doc: Union[str, bytes, Drawing]) -> List[str]:
        """Inspect and return all layer names present in the DXF drawing."""
        if isinstance(doc, str):
            drawing = cls._load_drawing_from_text(doc)
        elif isinstance(doc, bytes):
            drawing = cls._load_drawing_from_bytes(doc)
        else:
            drawing = doc
        return [layer.dxf.name for layer in drawing.layers]

    @classmethod
    def read_from_text(
        cls,
        dxf_text: str,
        target_layers: Optional[List[str]] = None,
        layer_pattern: Optional[str] = None,
        link_annotations: bool = True,
    ) -> DXFIngestionResult:
        """Extract spatial entities from an ASCII DXF string."""
        drawing = cls._load_drawing_from_text(dxf_text)
        return cls._extract_entities_from_drawing(
            drawing,
            target_layers=target_layers,
            layer_pattern=layer_pattern,
            link_annotations=link_annotations,
        )

    @classmethod
    def read_from_bytes(
        cls,
        dxf_bytes: bytes,
        target_layers: Optional[List[str]] = None,
        layer_pattern: Optional[str] = None,
        link_annotations: bool = True,
    ) -> DXFIngestionResult:
        """Extract spatial entities from DXF raw bytes."""
        drawing = cls._load_drawing_from_bytes(dxf_bytes)
        return cls._extract_entities_from_drawing(
            drawing,
            target_layers=target_layers,
            layer_pattern=layer_pattern,
            link_annotations=link_annotations,
        )

    @classmethod
    def read_from_file(
        cls,
        file_path: Union[str, Path],
        target_layers: Optional[List[str]] = None,
        layer_pattern: Optional[str] = None,
        link_annotations: bool = True,
    ) -> DXFIngestionResult:
        """Extract spatial entities from a DXF file path."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"DXF file not found: {path}")
        raw_bytes = path.read_bytes()
        return cls.read_from_bytes(
            raw_bytes,
            target_layers=target_layers,
            layer_pattern=layer_pattern,
            link_annotations=link_annotations,
        )

    @classmethod
    def _should_include_layer(
        cls,
        layer_name: str,
        target_layers: Optional[List[str]] = None,
        layer_pattern: Optional[str] = None,
    ) -> bool:
        """Evaluate whether a given layer matches the inclusion criteria."""
        if target_layers is not None:
            norm_targets = {l.upper() for l in target_layers}
            if layer_name.upper() not in norm_targets:
                return False

        if layer_pattern is not None:
            if not re.search(layer_pattern, layer_name, re.IGNORECASE):
                return False

        return True

    @classmethod
    def _extract_entities_from_drawing(
        cls,
        doc: Drawing,
        target_layers: Optional[List[str]] = None,
        layer_pattern: Optional[str] = None,
        link_annotations: bool = True,
    ) -> DXFIngestionResult:
        """Extract supported spatial entities from an ezdxf Drawing document."""
        msp = doc.modelspace()
        available_layers = [layer.dxf.name for layer in doc.layers]

        extracted_records: List[DXFEntityRecord] = []
        entity_counts: Dict[str, int] = {}
        layer_counts: Dict[str, int] = {}
        invalid_count = 0

        # Pass 1: Extract modelspace entities
        for entity in msp:
            layer_name = entity.dxf.layer
            if not cls._should_include_layer(layer_name, target_layers, layer_pattern):
                continue

            etype = entity.dxftype()
            handle = getattr(entity.dxf, "handle", "0")
            geom = None
            properties: Dict[str, Any] = {
                "color": getattr(entity.dxf, "color", 256),
                "linetype": getattr(entity.dxf, "linetype", "BYLAYER"),
            }

            if etype == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in entity.get_points(format="xy")]
                is_closed = bool(entity.is_closed)
                properties["is_closed"] = is_closed
                properties["vertex_count"] = len(pts)

                if len(pts) >= 3 and is_closed:
                    poly = Polygon(pts)
                    if poly.is_valid:
                        geom = poly
                        properties["area_sqm"] = float(poly.area)
                        properties["length_m"] = float(poly.length)
                    else:
                        geom = LineString(pts)
                        properties["topology_warning"] = "Self-intersecting polyline stored as LineString"
                        invalid_count += 1
                elif len(pts) >= 2:
                    geom = LineString(pts)
                    properties["length_m"] = float(geom.length)

            elif etype == "POLYLINE":
                pts = [
                    (float(v.dxf.location.x), float(v.dxf.location.y))
                    for v in entity.vertices
                ]
                is_closed = bool(entity.is_closed)
                properties["is_closed"] = is_closed
                properties["vertex_count"] = len(pts)

                if len(pts) >= 3 and is_closed:
                    poly = Polygon(pts)
                    if poly.is_valid:
                        geom = poly
                        properties["area_sqm"] = float(poly.area)
                        properties["length_m"] = float(poly.length)
                    else:
                        geom = LineString(pts)
                        properties["topology_warning"] = "Invalid polyline stored as LineString"
                        invalid_count += 1
                elif len(pts) >= 2:
                    geom = LineString(pts)
                    properties["length_m"] = float(geom.length)

            elif etype == "LINE":
                start = entity.dxf.start
                end = entity.dxf.end
                geom = LineString([(float(start.x), float(start.y)), (float(end.x), float(end.y))])
                properties["length_m"] = float(geom.length)

            elif etype == "POINT":
                loc = entity.dxf.location
                geom = Point(float(loc.x), float(loc.y))

            elif etype in ("TEXT", "MTEXT"):
                text_content = (
                    entity.dxf.text if etype == "TEXT" else entity.text
                )
                insert = entity.dxf.insert
                geom = Point(float(insert.x), float(insert.y))
                properties["text"] = text_content.strip()

            elif etype == "INSERT":
                # Block reference: capture insertion point and block definition name
                insert = entity.dxf.insert
                geom = Point(float(insert.x), float(insert.y))
                properties["block_name"] = entity.dxf.name
                properties["rotation"] = getattr(entity.dxf, "rotation", 0.0)

            if geom is not None:
                record = DXFEntityRecord(
                    entity_type=etype,
                    layer=layer_name,
                    geometry=geom,
                    handle=handle,
                    properties=properties,
                )
                extracted_records.append(record)
                entity_counts[etype] = entity_counts.get(etype, 0) + 1
                layer_counts[layer_name] = layer_counts.get(layer_name, 0) + 1

        # Pass 2: Spatial Attribute Linking (if requested)
        if link_annotations:
            cls._link_enclosed_text_labels(extracted_records)

        # Calculate combined bounds
        bounds = None
        if extracted_records:
            geoms = [r.geometry for r in extracted_records]
            all_minx = min(g.bounds[0] for g in geoms)
            all_miny = min(g.bounds[1] for g in geoms)
            all_maxx = max(g.bounds[2] for g in geoms)
            all_maxy = max(g.bounds[3] for g in geoms)
            bounds = (all_minx, all_miny, all_maxx, all_maxy)

        return DXFIngestionResult(
            entities=extracted_records,
            layers=available_layers,
            bounds=bounds,
            entity_counts=entity_counts,
            layer_counts=layer_counts,
            invalid_geometries_count=invalid_count,
        )

    @classmethod
    def _link_enclosed_text_labels(cls, records: List[DXFEntityRecord]) -> None:
        """Correlate point text labels inside enclosing cadastral polygons."""
        polygons = [r for r in records if isinstance(r.geometry, Polygon)]
        text_points = [
            r for r in records if r.entity_type in ("TEXT", "MTEXT") and "text" in r.properties
        ]

        if not polygons or not text_points:
            return

        for poly_rec in polygons:
            poly: Polygon = poly_rec.geometry
            enclosed_labels: List[str] = []

            for txt_rec in text_points:
                pt: Point = txt_rec.geometry
                if poly.contains(pt) or poly.touches(pt):
                    label = txt_rec.properties["text"]
                    enclosed_labels.append(label)
                    # Attempt khasra number extraction (e.g., '101', 'KHASRA 101', '101/1')
                    khasra_match = re.search(r"\b(\d+(?:[/-]\d+)?)\b", label)
                    if khasra_match and "khasra_number" not in poly_rec.properties:
                        poly_rec.properties["khasra_number"] = khasra_match.group(1)

            if enclosed_labels:
                poly_rec.properties["enclosed_labels"] = enclosed_labels
