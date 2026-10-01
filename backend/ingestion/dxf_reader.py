"""DXF Ingestion Reader using ezdxf for cadastral entity extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import ezdxf
from ezdxf.document import Drawing
from ezdxf.lldxf.const import DXFStructureError, DXFValueError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union


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


class DXFIngestionReader:
    """Reader for extracting cadastral boundaries and spatial markers from DXF drawings."""

    @staticmethod
    def _load_drawing_from_text(dxf_text: str) -> Drawing:
        """Parse Drawing object from ASCII DXF string."""
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
        # Try decoding as standard UTF-8 or Latin-1
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
    ) -> DXFIngestionResult:
        """Extract spatial entities from an ASCII DXF string."""
        drawing = cls._load_drawing_from_text(dxf_text)
        return cls._extract_entities_from_drawing(drawing, target_layers=target_layers)

    @classmethod
    def read_from_bytes(
        cls,
        dxf_bytes: bytes,
        target_layers: Optional[List[str]] = None,
    ) -> DXFIngestionResult:
        """Extract spatial entities from DXF raw bytes."""
        drawing = cls._load_drawing_from_bytes(dxf_bytes)
        return cls._extract_entities_from_drawing(drawing, target_layers=target_layers)

    @classmethod
    def read_from_file(
        cls,
        file_path: Union[str, Path],
        target_layers: Optional[List[str]] = None,
    ) -> DXFIngestionResult:
        """Extract spatial entities from a DXF file path."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"DXF file not found: {path}")
        raw_bytes = path.read_bytes()
        return cls.read_from_bytes(raw_bytes, target_layers=target_layers)

    @classmethod
    def _extract_entities_from_drawing(
        cls,
        doc: Drawing,
        target_layers: Optional[List[str]] = None,
    ) -> DXFIngestionResult:
        """Extract supported spatial entities from an ezdxf Drawing document."""
        msp = doc.modelspace()
        available_layers = [layer.dxf.name for layer in doc.layers]
        layer_filter: Optional[Set[str]] = (
            {l.upper() for l in target_layers} if target_layers else None
        )

        extracted_records: List[DXFEntityRecord] = []
        entity_counts: Dict[str, int] = {}

        for entity in msp:
            layer_name = entity.dxf.layer
            if layer_filter is not None and layer_name.upper() not in layer_filter:
                continue

            etype = entity.dxftype()
            handle = getattr(entity.dxf, "handle", "0")
            geom = None
            properties: Dict[str, Any] = {}

            if etype == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in entity.get_points(format="xy")]
                if len(pts) >= 3 and entity.is_closed:
                    geom = Polygon(pts)
                    if not geom.is_valid:
                        geom = LineString(pts)
                elif len(pts) >= 2:
                    geom = LineString(pts)

            elif etype == "POLYLINE":
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                if len(pts) >= 3 and entity.is_closed:
                    geom = Polygon(pts)
                    if not geom.is_valid:
                        geom = LineString(pts)
                elif len(pts) >= 2:
                    geom = LineString(pts)

            elif etype == "POINT":
                loc = entity.dxf.location
                geom = Point(loc.x, loc.y)

            elif etype in ("TEXT", "MTEXT"):
                text_content = (
                    entity.dxf.text if etype == "TEXT" else entity.text
                )
                insert = entity.dxf.insert
                geom = Point(insert.x, insert.y)
                properties["text"] = text_content

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
        )
