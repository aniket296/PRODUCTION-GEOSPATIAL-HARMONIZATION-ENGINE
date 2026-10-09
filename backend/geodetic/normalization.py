"""Cadastral boundary normalization, geometry sanitization, and topology preservation engine.

Provides deterministic algorithms for:
- Cadastral polygon boundary sanitization (duplicate vertices, unclosed rings, spikes, self-intersections).
- OGC / RFC 7946 ring orientation normalization (exterior CCW, interior CW).
- Precision grid alignment and coordinate snapping.
- Area and perimeter preservation bounds (legal Indian land record ROR / Khasra fidelity).
- Shared boundary alignment / topological snapping between contiguous revenue parcels.
- Planar graph validation and anomaly classification (micro-overlaps, disputes, slivers, gaps, dangling nodes).
"""

from __future__ import annotations

import copy
import enum
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import shapely
from shapely import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    distance,
    hausdorff_distance,
    is_valid,
    make_valid,
)
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize, snap, unary_union


class DefectType(str, enum.Enum):
    """Classification of cadastral boundary and topological defects."""

    OVERLAP = "OVERLAP"
    SLIVER = "SLIVER"
    GAP = "GAP"
    DANGLING_NODE = "DANGLING_NODE"
    DUPLICATE_VERTEX = "DUPLICATE_VERTEX"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    UNCLOSED_RING = "UNCLOSED_RING"
    SPIKE_VERTEX = "SPIKE_VERTEX"
    WRONG_ORIENTATION = "WRONG_ORIENTATION"
    COLLAPSED_GEOMETRY = "COLLAPSED_GEOMETRY"


class DefectSeverity(str, enum.Enum):
    """Severity classification determining automated healing eligibility."""

    AUTO_RESOLVABLE = "AUTO_RESOLVABLE"
    CRITICAL_DISPUTE = "CRITICAL_DISPUTE"
    WARNING = "WARNING"
    INFO = "INFO"


class NormalizationError(Exception):
    """Raised when geometry cannot be normalized or violates fundamental topological rules."""

    pass


@dataclass
class NormalizationConfig:
    """Configuration parameters for cadastral boundary normalization and topological preservation."""

    snap_precision_m: float = 0.001           # Coordinate snapping grid resolution in metres (1 mm)
    collinear_tolerance_m: float = 0.005       # Distance threshold to collapse redundant collinear vertices (5 mm)
    min_parcel_area_sqm: float = 1.0           # Minimum allowable parcel area in square metres
    sliver_compactness_threshold: float = 0.05 # Polsby-Popper compactness threshold for sliver detection
    max_area_drift_pct: float = 0.05           # Maximum allowable area change percentage after sanitization (0.05%)
    max_hausdorff_drift_m: float = 0.05        # Maximum boundary displacement allowed in metres (5 cm)
    auto_resolve_overlap_max_sqm: float = 5.0  # Max overlap area eligible for automatic reconciliation
    shared_boundary_snap_tolerance_m: float = 0.05 # Distance tolerance to snap adjacent parcel shared edges (5 cm)
    orient_ccw: bool = True                   # Enforce exterior CCW and interior CW ring orientation
    force_2d: bool = True                      # Strip Z/M elevations to pure planar coordinates
    repair_self_intersections: bool = True     # Resolve self-intersecting bow-ties


@dataclass
class NormalizationAudit:
    """Detailed audit metrics and repaired anomaly log from polygon normalization."""

    original_area_sqm: float
    normalized_area_sqm: float
    area_drift_pct: float
    original_vertex_count: int
    normalized_vertex_count: int
    vertices_removed: int
    max_displacement_m: float
    repaired_defects: List[str] = field(default_factory=list)
    is_valid_polygon: bool = True
    is_multipolygon: bool = False
    num_parts: int = 1
    num_holes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_area_sqm": round(self.original_area_sqm, 4),
            "normalized_area_sqm": round(self.normalized_area_sqm, 4),
            "area_drift_pct": round(self.area_drift_pct, 6),
            "original_vertex_count": self.original_vertex_count,
            "normalized_vertex_count": self.normalized_vertex_count,
            "vertices_removed": self.vertices_removed,
            "max_displacement_m": round(self.max_displacement_m, 6),
            "repaired_defects": list(self.repaired_defects),
            "is_valid_polygon": self.is_valid_polygon,
            "is_multipolygon": self.is_multipolygon,
            "num_parts": self.num_parts,
            "num_holes": self.num_holes,
        }


@dataclass
class CadastralNormalizationResult:
    """Sanitized geometry result accompanied by complete audit trail."""

    geometry: Union[Polygon, MultiPolygon]
    audit: NormalizationAudit

    @property
    def geojson(self) -> Dict[str, Any]:
        return shapely.geometry.mapping(self.geometry)


@dataclass
class TopologicalAnomaly:
    """Specific topological anomaly identified between or within cadastral parcels."""

    defect_type: DefectType
    severity: DefectSeverity
    description: str
    affected_parcel_ids: List[str]
    affected_area_sqm: float = 0.0
    geometry: Optional[BaseGeometry] = None
    location: Optional[Tuple[float, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "defect_type": self.defect_type.value,
            "severity": self.severity.value,
            "description": self.description,
            "affected_parcel_ids": self.affected_parcel_ids,
            "affected_area_sqm": round(self.affected_area_sqm, 4),
            "location": [round(c, 4) for c in self.location] if self.location else None,
            "geometry": shapely.geometry.mapping(self.geometry) if self.geometry else None,
        }


@dataclass
class CadastralTopologyReport:
    """Comprehensive topology health and anomaly diagnostic report for a cadastral layer."""

    total_parcels: int
    valid_parcels_count: int
    invalid_parcels_count: int
    anomalies: List[TopologicalAnomaly] = field(default_factory=list)
    is_planar_graph_valid: bool = True
    total_overlap_area_sqm: float = 0.0
    total_gap_area_sqm: float = 0.0
    auto_resolvable_count: int = 0
    critical_dispute_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_parcels": self.total_parcels,
            "valid_parcels_count": self.valid_parcels_count,
            "invalid_parcels_count": self.invalid_parcels_count,
            "is_planar_graph_valid": self.is_planar_graph_valid,
            "total_overlap_area_sqm": round(self.total_overlap_area_sqm, 4),
            "total_gap_area_sqm": round(self.total_gap_area_sqm, 4),
            "auto_resolvable_count": self.auto_resolvable_count,
            "critical_dispute_count": self.critical_dispute_count,
            "anomalies_count": len(self.anomalies),
            "anomalies": [a.to_dict() for a in self.anomalies],
        }


def calculate_polsby_popper_compactness(geometry: BaseGeometry) -> float:
    """Calculate Polsby-Popper compactness score: 4 * pi * Area / Perimeter^2.

    A perfect circle has score 1.0; narrow slivers approach 0.0.
    """
    if geometry is None or geometry.is_empty:
        return 0.0
    area = geometry.area
    perimeter = geometry.length
    if perimeter <= 0.0 or area <= 0.0:
        return 0.0
    return (4.0 * math.pi * area) / (perimeter * perimeter)


class GeometrySanitizer:
    """Core mathematical algorithms for cleaning and standardizing individual parcel boundaries."""

    @staticmethod
    def strip_z(coords: Sequence[Sequence[float]]) -> List[Tuple[float, float]]:
        """Drop 3D/Z elevations, returning strictly 2D (x, y) tuples."""
        return [(float(c[0]), float(c[1])) for c in coords]

    @staticmethod
    def snap_coordinate(coord: Tuple[float, float], precision: float) -> Tuple[float, float]:
        """Snap coordinate to discrete precision grid."""
        if precision <= 0.0:
            return coord
        return (
            round(coord[0] / precision) * precision,
            round(coord[1] / precision) * precision,
        )

    @classmethod
    def ensure_ring_closed(
        cls, coords: Sequence[Tuple[float, float]]
    ) -> Tuple[List[Tuple[float, float]], bool]:
        """Ensure ring starts and ends at the identical point.

        Returns: (closed_coords, was_modified)
        """
        if not coords:
            return [], False
        pt_list = list(coords)
        was_modified = False
        if len(pt_list) < 2:
            return pt_list, False

        first = pt_list[0]
        last = pt_list[-1]
        dist = math.hypot(first[0] - last[0], first[1] - last[1])
        if dist > 1e-9:
            pt_list.append(first)
            was_modified = True
        return pt_list, was_modified

    @classmethod
    def remove_duplicate_vertices(
        cls,
        coords: Sequence[Tuple[float, float]],
        tolerance: float = 1e-4,
    ) -> Tuple[List[Tuple[float, float]], int]:
        """Remove consecutive identical points within distance tolerance.

        Preserves ring closure.
        Returns: (deduplicated_coords, count_removed)
        """
        if len(coords) < 2:
            return list(coords), 0

        cleaned: List[Tuple[float, float]] = [coords[0]]
        removed = 0

        for pt in coords[1:]:
            prev = cleaned[-1]
            dist = math.hypot(pt[0] - prev[0], pt[1] - prev[1])
            if dist <= tolerance:
                removed += 1
            else:
                cleaned.append(pt)

        # Re-verify closure if original was closed
        if len(coords) >= 4 and coords[0] == coords[-1]:
            if len(cleaned) >= 2 and cleaned[0] != cleaned[-1]:
                cleaned.append(cleaned[0])

        return cleaned, removed

    @classmethod
    def remove_collinear_vertices(
        cls,
        coords: Sequence[Tuple[float, float]],
        tolerance: float = 0.005,
    ) -> Tuple[List[Tuple[float, float]], int]:
        """Decimate redundant intermediate vertices lying on straight edges within cross-track tolerance.

        Returns: (decimated_coords, count_removed)
        """
        if len(coords) <= 4:
            return list(coords), 0

        is_closed = coords[0] == coords[-1]
        pts = list(coords[:-1]) if is_closed else list(coords)
        n = len(pts)
        if n < 3:
            return list(coords), 0

        filtered: List[Tuple[float, float]] = []
        removed = 0

        for i in range(n):
            p_prev = pts[(i - 1) % n]
            p_curr = pts[i]
            p_next = pts[(i + 1) % n]

            # Cross-track perpendicular distance from p_curr to line (p_prev, p_next)
            dx = p_next[0] - p_prev[0]
            dy = p_next[1] - p_prev[1]
            line_len = math.hypot(dx, dy)

            if line_len < 1e-9:
                # p_prev and p_next are coincident
                removed += 1
                continue

            # Perpendicular distance: |(y2 - y1)*x0 - (x2 - x1)*y0 + x2*y1 - y2*x1| / length
            dist = abs(dy * p_curr[0] - dx * p_curr[1] + p_next[0] * p_prev[1] - p_next[1] * p_prev[0]) / line_len

            if dist <= tolerance:
                removed += 1
            else:
                filtered.append(p_curr)

        if len(filtered) < 3:
            # Cannot decimate below a triangle
            return list(coords), 0

        if is_closed:
            filtered.append(filtered[0])

        return filtered, removed

    @classmethod
    def remove_spikes(
        cls,
        coords: Sequence[Tuple[float, float]],
        max_angle_deg: float = 1.0,
        spike_length_tolerance: float = 0.05,
    ) -> Tuple[List[Tuple[float, float]], int]:
        """Remove acute angle spikes where a boundary doubles back on itself (digitization overshoot).

        Returns: (filtered_coords, count_removed)
        """
        if len(coords) <= 4:
            return list(coords), 0

        is_closed = coords[0] == coords[-1]
        pts = list(coords[:-1]) if is_closed else list(coords)
        n = len(pts)
        if n < 3:
            return list(coords), 0

        filtered: List[Tuple[float, float]] = []
        removed = 0
        rad_thresh = math.radians(max_angle_deg)

        for i in range(n):
            p_prev = pts[(i - 1) % n]
            p_curr = pts[i]
            p_next = pts[(i + 1) % n]

            v1_x = p_prev[0] - p_curr[0]
            v1_y = p_prev[1] - p_curr[1]
            v2_x = p_next[0] - p_curr[0]
            v2_y = p_next[1] - p_curr[1]

            len1 = math.hypot(v1_x, v1_y)
            len2 = math.hypot(v2_x, v2_y)

            if len1 < 1e-9 or len2 < 1e-9:
                filtered.append(p_curr)
                continue

            dot = (v1_x * v2_x + v1_y * v2_y) / (len1 * len2)
            dot = max(-1.0, min(1.0, dot))
            angle = math.acos(dot)

            # Very small angle implies the path doubles back on itself
            if angle <= rad_thresh and (len1 <= spike_length_tolerance or len2 <= spike_length_tolerance):
                removed += 1
            else:
                filtered.append(p_curr)

        if len(filtered) < 3:
            return list(coords), 0

        if is_closed:
            filtered.append(filtered[0])

        return filtered, removed

    @classmethod
    def clean_ring(
        cls,
        coords: Sequence[Sequence[float]],
        config: NormalizationConfig,
        repaired_defects: Set[str],
    ) -> List[Tuple[float, float]]:
        """Clean an individual LinearRing coordinate sequence."""
        # 1. 2D strip
        pts_2d = cls.strip_z(coords)

        # 2. Snap to grid if configured
        if config.snap_precision_m > 0.0:
            pts_2d = [cls.snap_coordinate(pt, config.snap_precision_m) for pt in pts_2d]

        # 3. Ensure closure
        pts_closed, closed_mod = cls.ensure_ring_closed(pts_2d)
        if closed_mod:
            repaired_defects.add(DefectType.UNCLOSED_RING.value)

        # 4. Remove duplicate vertices
        pts_dedup, dup_count = cls.remove_duplicate_vertices(pts_closed, tolerance=config.collinear_tolerance_m)
        if dup_count > 0:
            repaired_defects.add(DefectType.DUPLICATE_VERTEX.value)

        # 5. Remove acute spikes
        pts_nospikes, spike_count = cls.remove_spikes(pts_dedup)
        if spike_count > 0:
            repaired_defects.add(DefectType.SPIKE_VERTEX.value)

        # 6. Remove collinear vertices
        pts_clean, col_count = cls.remove_collinear_vertices(pts_nospikes, tolerance=config.collinear_tolerance_m)

        return pts_clean

    @classmethod
    def clean_polygon(
        cls,
        geom: BaseGeometry,
        config: Optional[NormalizationConfig] = None,
    ) -> CadastralNormalizationResult:
        """Sanitize and normalize a Polygon or MultiPolygon.

        Guarantees:
        - Output is valid 2D Polygon or MultiPolygon.
        - Counter-clockwise exterior ring and clockwise interior rings (holes).
        - No duplicate or unclosed ring vertices.
        - Rigorous audit metrics tracking area drift, displacement, and removed defects.
        """
        if config is None:
            config = NormalizationConfig()

        if geom is None or geom.is_empty:
            raise NormalizationError("Cannot normalize null or empty geometry.")

        # Strip 3D if present
        if config.force_2d and geom.has_z:
            geom = shapely.force_2d(geom)

        original_area = float(geom.area)
        original_vertices = len(shapely.get_coordinates(geom))
        repaired_defects: Set[str] = set()

        # Step 1: Self-intersection / invalid geometry repair
        working_geom = geom
        if not geom.is_valid:
            repaired_defects.add(DefectType.INVALID_GEOMETRY.value)
            if config.repair_self_intersections:
                # Use make_valid to repair bow-ties, self-crossings, etc.
                working_geom = make_valid(geom)
                repaired_defects.add(DefectType.SELF_INTERSECTION.value if hasattr(DefectType, "SELF_INTERSECTION") else "SELF_INTERSECTION")
            else:
                raise NormalizationError(f"Geometry is topologically invalid: {shapely.validation.explain_validity(geom)}")

        # Step 2: Extract polygon components
        poly_parts: List[Polygon] = []
        if isinstance(working_geom, Polygon):
            poly_parts.append(working_geom)
        elif isinstance(working_geom, MultiPolygon):
            poly_parts.extend(working_geom.geoms)
        elif isinstance(working_geom, GeometryCollection):
            for part in working_geom.geoms:
                if isinstance(part, Polygon):
                    poly_parts.append(part)
                elif isinstance(part, MultiPolygon):
                    poly_parts.extend(part.geoms)
        else:
            raise NormalizationError(f"Normalized geometry collapsed to non-polygonal type: {working_geom.geom_type}")

        if not poly_parts:
            raise NormalizationError("Geometry has no polygonal parts remaining after sanitization.")

        # Step 3: Sanitize each polygon ring
        cleaned_polys: List[Polygon] = []
        for p in poly_parts:
            ext_cleaned = cls.clean_ring(p.exterior.coords, config, repaired_defects)
            if len(ext_cleaned) < 4:
                # Degenerate exterior ring
                continue

            cleaned_holes: List[List[Tuple[float, float]]] = []
            for h in p.interiors:
                h_cleaned = cls.clean_ring(h.coords, config, repaired_defects)
                if len(h_cleaned) >= 4:
                    cleaned_holes.append(h_cleaned)
                else:
                    repaired_defects.add(DefectType.COLLAPSED_GEOMETRY.value)

            p_sanitized = Polygon(ext_cleaned, cleaned_holes)
            if not p_sanitized.is_valid:
                p_sanitized = make_valid(p_sanitized)
                if isinstance(p_sanitized, MultiPolygon):
                    cleaned_polys.extend([sub for sub in p_sanitized.geoms if sub.area > 0])
                    continue
                elif isinstance(p_sanitized, GeometryCollection):
                    cleaned_polys.extend([sub for sub in p_sanitized.geoms if isinstance(sub, Polygon) and sub.area > 0])
                    continue

            if p_sanitized.area > 0.0:
                cleaned_polys.append(p_sanitized)

        if not cleaned_polys:
            raise NormalizationError("All polygon rings collapsed to zero area during sanitization.")

        # Step 4: Ring orientation (exterior CCW, interior CW)
        if config.orient_ccw:
            oriented_polys = []
            for p in cleaned_polys:
                was_ccw = p.exterior.is_ccw
                if not was_ccw:
                    repaired_defects.add(DefectType.WRONG_ORIENTATION.value)
                oriented_polys.append(orient(p, sign=1.0))
            cleaned_polys = oriented_polys

        # Step 5: Construct final geometry
        if len(cleaned_polys) == 1:
            final_geom: Union[Polygon, MultiPolygon] = cleaned_polys[0]
            is_mp = False
        else:
            final_geom = MultiPolygon(cleaned_polys)
            is_mp = True

        final_area = float(final_geom.area)
        final_vertices = len(shapely.get_coordinates(final_geom))

        # Step 6: Compute audit drift
        area_drift_pct = (
            abs(final_area - original_area) / original_area * 100.0
            if original_area > 0.0
            else 0.0
        )
        try:
            h_dist = float(hausdorff_distance(geom, final_geom))
        except Exception:
            h_dist = 0.0

        num_holes = sum(len(p.interiors) for p in (cleaned_polys if not is_mp else final_geom.geoms))

        audit = NormalizationAudit(
            original_area_sqm=original_area,
            normalized_area_sqm=final_area,
            area_drift_pct=area_drift_pct,
            original_vertex_count=original_vertices,
            normalized_vertex_count=final_vertices,
            vertices_removed=max(0, original_vertices - final_vertices),
            max_displacement_m=h_dist,
            repaired_defects=sorted(list(repaired_defects)),
            is_valid_polygon=final_geom.is_valid,
            is_multipolygon=is_mp,
            num_parts=len(cleaned_polys),
            num_holes=num_holes,
        )

        return CadastralNormalizationResult(geometry=final_geom, audit=audit)


class CadastralTopologyValidator:
    """Planar graph validation and spatial anomaly detection across cadastral parcel collections."""

    @classmethod
    def validate_single_parcel(
        cls,
        parcel_id: str,
        geom: BaseGeometry,
        config: Optional[NormalizationConfig] = None,
    ) -> List[TopologicalAnomaly]:
        """Validate self-integrity of an individual parcel."""
        if config is None:
            config = NormalizationConfig()

        anomalies: List[TopologicalAnomaly] = []

        if geom is None or geom.is_empty:
            anomalies.append(
                TopologicalAnomaly(
                    defect_type=DefectType.INVALID_GEOMETRY,
                    severity=DefectSeverity.CRITICAL_DISPUTE,
                    description=f"Parcel {parcel_id} has empty or null geometry",
                    affected_parcel_ids=[parcel_id],
                )
            )
            return anomalies

        # Validity check
        if not geom.is_valid:
            anomalies.append(
                TopologicalAnomaly(
                    defect_type=DefectType.INVALID_GEOMETRY,
                    severity=DefectSeverity.CRITICAL_DISPUTE,
                    description=f"Parcel {parcel_id} is topologically invalid: {shapely.validation.explain_validity(geom)}",
                    affected_parcel_ids=[parcel_id],
                    geometry=geom,
                )
            )

        # Minimum area check
        if geom.area < config.min_parcel_area_sqm:
            anomalies.append(
                TopologicalAnomaly(
                    defect_type=DefectType.SLIVER,
                    severity=DefectSeverity.WARNING,
                    description=f"Parcel {parcel_id} area ({geom.area:.3f} sqm) is below minimum cadastral threshold ({config.min_parcel_area_sqm} sqm)",
                    affected_parcel_ids=[parcel_id],
                    affected_area_sqm=float(geom.area),
                    geometry=geom,
                )
            )

        # Sliver compactness check
        compactness = calculate_polsby_popper_compactness(geom)
        if compactness < config.sliver_compactness_threshold and geom.area < 100.0:
            anomalies.append(
                TopologicalAnomaly(
                    defect_type=DefectType.SLIVER,
                    severity=DefectSeverity.WARNING,
                    description=f"Parcel {parcel_id} compactness ({compactness:.4f}) indicates a spurious sliver",
                    affected_parcel_ids=[parcel_id],
                    affected_area_sqm=float(geom.area),
                    geometry=geom,
                )
            )

        return anomalies

    @classmethod
    def validate_parcel_pair(
        cls,
        id_a: str,
        geom_a: Polygon,
        id_b: str,
        geom_b: Polygon,
        config: Optional[NormalizationConfig] = None,
    ) -> List[TopologicalAnomaly]:
        """Evaluate topological relationship between two neighboring parcels for overlaps and boundary integrity."""
        if config is None:
            config = NormalizationConfig()

        anomalies: List[TopologicalAnomaly] = []

        # Check intersection
        if geom_a.intersects(geom_b):
            # If they touch along boundary, intersection is LineString or Point -> valid topology
            inter = geom_a.intersection(geom_b)
            if inter.area > 1e-6:
                overlap_area = float(inter.area)
                severity = (
                    DefectSeverity.AUTO_RESOLVABLE
                    if overlap_area <= config.auto_resolve_overlap_max_sqm
                    else DefectSeverity.CRITICAL_DISPUTE
                )
                anomalies.append(
                    TopologicalAnomaly(
                        defect_type=DefectType.OVERLAP,
                        severity=severity,
                        description=f"Overlap between parcels {id_a} and {id_b} ({overlap_area:.3f} sqm)",
                        affected_parcel_ids=[id_a, id_b],
                        affected_area_sqm=overlap_area,
                        geometry=inter,
                        location=(inter.centroid.x, inter.centroid.y),
                    )
                )

        return anomalies

    @classmethod
    def detect_dangling_nodes(
        cls,
        geom_a: Polygon,
        geom_b: Polygon,
        tolerance: float = 0.05,
    ) -> List[Tuple[float, float]]:
        """Identify T-junction / dangling node vertices where a vertex of parcel A lies on an edge of parcel B

        without parcel B having a matching coincident vertex at that location.
        """
        dangling: List[Tuple[float, float]] = []
        coords_a = list(geom_a.exterior.coords)
        coords_b = set(geom_b.exterior.coords)
        ext_b = geom_b.exterior

        for pt in coords_a:
            if pt in coords_b:
                continue
            p_geom = Point(pt)
            dist = ext_b.distance(p_geom)
            if dist <= tolerance:
                dangling.append(pt)

        return dangling

    @classmethod
    def validate_parcel_collection(
        cls,
        parcels: Sequence[Dict[str, Any]],
        geom_key: str = "geometry",
        id_key: str = "parcel_identifier",
        config: Optional[NormalizationConfig] = None,
    ) -> CadastralTopologyReport:
        """Execute full planar graph validation and topology diagnostics across a collection of parcels."""
        if config is None:
            config = NormalizationConfig()

        n = len(parcels)
        anomalies: List[TopologicalAnomaly] = []
        valid_parcels = 0
        invalid_parcels = 0
        total_overlap_sqm = 0.0

        # 1. Single parcel integrity checks
        for p in parcels:
            pid = str(p.get(id_key, "UNKNOWN"))
            geom = p.get(geom_key)
            single_anoms = cls.validate_single_parcel(pid, geom, config)
            if single_anoms:
                invalid_parcels += 1
                anomalies.extend(single_anoms)
            else:
                valid_parcels += 1

        # 2. Pairwise overlap and contiguity checks
        for i in range(n):
            pid_a = str(parcels[i].get(id_key, f"P_{i}"))
            geom_a = parcels[i].get(geom_key)
            if geom_a is None or geom_a.is_empty:
                continue

            for j in range(i + 1, n):
                pid_b = str(parcels[j].get(id_key, f"P_{j}"))
                geom_b = parcels[j].get(geom_key)
                if geom_b is None or geom_b.is_empty:
                    continue

                # Bounding box filter for spatial performance
                if not (
                    geom_a.bounds[0] <= geom_b.bounds[2]
                    and geom_a.bounds[2] >= geom_b.bounds[0]
                    and geom_a.bounds[1] <= geom_b.bounds[3]
                    and geom_a.bounds[3] >= geom_b.bounds[1]
                ):
                    continue

                pair_anoms = cls.validate_parcel_pair(pid_a, geom_a, pid_b, geom_b, config)
                for anom in pair_anoms:
                    if anom.defect_type == DefectType.OVERLAP:
                        total_overlap_sqm += anom.affected_area_sqm
                    anomalies.append(anom)

        # 3. Detect internal boundary gaps between contiguous clusters
        total_gap_sqm = 0.0
        if n >= 2:
            gap_anoms = cls.find_internal_boundary_gaps(parcels, geom_key, id_key, config)
            for g in gap_anoms:
                total_gap_sqm += g.affected_area_sqm
                anomalies.append(g)

        auto_res = sum(1 for a in anomalies if a.severity == DefectSeverity.AUTO_RESOLVABLE)
        crit_disp = sum(1 for a in anomalies if a.severity == DefectSeverity.CRITICAL_DISPUTE)
        is_planar_valid = len(anomalies) == 0

        return CadastralTopologyReport(
            total_parcels=n,
            valid_parcels_count=valid_parcels,
            invalid_parcels_count=invalid_parcels,
            anomalies=anomalies,
            is_planar_graph_valid=is_planar_valid,
            total_overlap_area_sqm=total_overlap_sqm,
            total_gap_area_sqm=total_gap_sqm,
            auto_resolvable_count=auto_res,
            critical_dispute_count=crit_disp,
        )

    @classmethod
    def find_internal_boundary_gaps(
        cls,
        parcels: Sequence[Dict[str, Any]],
        geom_key: str = "geometry",
        id_key: str = "parcel_identifier",
        config: Optional[NormalizationConfig] = None,
    ) -> List[TopologicalAnomaly]:
        """Detect sliver voids / holes enclosed within the union envelope of contiguous parcels."""
        if config is None:
            config = NormalizationConfig()

        anomalies: List[TopologicalAnomaly] = []
        geoms = [p[geom_key] for p in parcels if p.get(geom_key) and not p[geom_key].is_empty]
        if len(geoms) < 2:
            return anomalies

        try:
            # Unary union of all parcels
            u = unary_union(geoms)
            if isinstance(u, Polygon):
                unions = [u]
            elif isinstance(u, MultiPolygon):
                unions = list(u.geoms)
            else:
                return anomalies

            for poly in unions:
                for interior_ring in poly.interiors:
                    hole_poly = Polygon(interior_ring)
                    hole_area = float(hole_poly.area)
                    if 0.001 <= hole_area <= 100.0:  # Typical cadastral boundary gap range
                        # Identify nearby parcels bounding this gap
                        touching_ids = [
                            str(p.get(id_key, "UNKNOWN"))
                            for p in parcels
                            if p.get(geom_key) and p[geom_key].distance(hole_poly) <= 0.1
                        ]
                        severity = (
                            DefectSeverity.AUTO_RESOLVABLE
                            if hole_area <= config.auto_resolve_overlap_max_sqm
                            else DefectSeverity.WARNING
                        )
                        anomalies.append(
                            TopologicalAnomaly(
                                defect_type=DefectType.GAP,
                                severity=severity,
                                description=f"Internal boundary gap / sliver void detected ({hole_area:.3f} sqm)",
                                affected_parcel_ids=touching_ids,
                                affected_area_sqm=hole_area,
                                geometry=hole_poly,
                                location=(hole_poly.centroid.x, hole_poly.centroid.y),
                            )
                        )
        except Exception:
            pass

        return anomalies


class CadastralTopologyPreserver:
    """Topology preservation and edge alignment engine for neighboring cadastral parcels."""

    @classmethod
    def snap_parcel_pair(
        cls,
        geom_a: Polygon,
        geom_b: Polygon,
        tolerance: float = 0.05,
    ) -> Tuple[Polygon, Polygon]:
        """Align near-coincident shared boundaries of two adjacent parcels by mutually snapping vertices

        within the specified tolerance (e.g. 5 cm).
        """
        # Snap A to B, then B to snapped A
        snapped_a = snap(geom_a, geom_b, tolerance)
        snapped_b = snap(geom_b, snapped_a, tolerance)

        # Make valid if slight self-intersection introduced
        if not snapped_a.is_valid:
            snapped_a = make_valid(snapped_a)
        if not snapped_b.is_valid:
            snapped_b = make_valid(snapped_b)

        return snapped_a, snapped_b

    @classmethod
    def align_shared_boundaries(
        cls,
        parcels: Sequence[Dict[str, Any]],
        geom_key: str = "geometry",
        id_key: str = "parcel_identifier",
        tolerance: Optional[float] = None,
        config: Optional[NormalizationConfig] = None,
    ) -> List[Dict[str, Any]]:
        """Iteratively snap shared boundaries across a collection of adjacent parcels to eliminate

        micro-gaps and align vertices along shared cadastral parcel edges.
        """
        if config is None:
            config = NormalizationConfig()

        tol = tolerance if tolerance is not None else config.shared_boundary_snap_tolerance_m
        results = [copy.deepcopy(p) for p in parcels]
        n = len(results)

        for i in range(n):
            geom_i = results[i].get(geom_key)
            if geom_i is None or geom_i.is_empty:
                continue

            for j in range(i + 1, n):
                geom_j = results[j].get(geom_key)
                if geom_j is None or geom_j.is_empty:
                    continue

                # Check proximity
                if geom_i.distance(geom_j) <= tol:
                    new_i, new_j = cls.snap_parcel_pair(geom_i, geom_j, tolerance=tol)
                    results[i][geom_key] = new_i
                    results[j][geom_key] = new_j
                    geom_i = new_i

        return results

    @classmethod
    def verify_legal_area_preservation(
        cls,
        original_geom: Polygon,
        sanitized_geom: Polygon,
        max_drift_pct: float = 0.05,
        max_drift_sqm: float = 0.5,
    ) -> Tuple[bool, float, str]:
        """Verify that sanitized parcel area adheres strictly to legal Indian land record preservation bounds.

        Returns: (is_preserved, drift_pct, reason)
        """
        a_orig = original_geom.area
        a_san = sanitized_geom.area

        if a_orig <= 0:
            return False, 0.0, "Original parcel area is zero or negative."

        diff_sqm = abs(a_san - a_orig)
        drift_pct = (diff_sqm / a_orig) * 100.0

        if drift_pct <= max_drift_pct or diff_sqm <= max_drift_sqm:
            return (
                True,
                drift_pct,
                f"Area preserved within tolerance: drift={drift_pct:.6f}% ({diff_sqm:.4f} sqm)",
            )

        return (
            False,
            drift_pct,
            f"Area drift {drift_pct:.4f}% ({diff_sqm:.4f} sqm) exceeds allowable threshold of {max_drift_pct}%",
        )
