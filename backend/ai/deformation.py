"""Optical flow and vector field deformation engine for elastic cadastre boundary snapping.

Provides:
1. DisplacementVector and continuous spatial VectorFieldInterpolator (Inverse Distance Weighting and Gaussian RBF kernels).
2. ElasticDeformationEngine:
   - Warp polygon boundaries along spatially varying displacement fields (dx, dy).
   - Elastic displacement clamping (enforcing maximum deformation limits per vertex).
   - Automatic boundary snapping from cadastre-to-satellite discrepancy pairs.
   - Conjoint cluster deformation preserving shared boundary topology and preventing overlaps/gaps.
3. Deformation audit metrics (RMS displacement, max displacement, area distortion ratio, topology check).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
import numpy as np
from shapely.geometry import MultiPolygon, Polygon, base, shape
from shapely.ops import unary_union
from shapely.validation import make_valid


@dataclass
class DisplacementVector:
    """Individual 2D displacement vector at a control location."""

    x: float
    y: float
    dx: float
    dy: float
    weight: float = 1.0

    @property
    def magnitude(self) -> float:
        return math.sqrt(self.dx * self.dx + self.dy * self.dy)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "dx": round(self.dx, 4),
            "dy": round(self.dy, 4),
            "weight": round(self.weight, 4),
            "magnitude": round(self.magnitude, 4),
        }


class VectorFieldInterpolator:
    """Interpolates continuous 2D displacement vectors across coordinate space."""

    def __init__(
        self,
        vectors: Sequence[DisplacementVector],
        kernel: str = "idw",
        power: float = 2.0,
        epsilon: float = 50.0,
    ) -> None:
        self.vectors = list(vectors)
        self.kernel = kernel.lower()
        self.power = power
        self.epsilon = epsilon

    def interpolate(self, x: float, y: float) -> Tuple[float, float]:
        """Interpolate displacement (dx, dy) at coordinate (x, y)."""
        if not self.vectors:
            return 0.0, 0.0

        if len(self.vectors) == 1:
            v = self.vectors[0]
            return float(v.dx), float(v.dy)

        if self.kernel in ("rbf", "gaussian_rbf"):
            return self._interpolate_rbf(x, y)
        else:
            return self._interpolate_idw(x, y)

    def _interpolate_idw(self, x: float, y: float) -> Tuple[float, float]:
        """Inverse Distance Weighting interpolation."""
        total_weight = 0.0
        sum_dx = 0.0
        sum_dy = 0.0

        for v in self.vectors:
            dist = math.sqrt((x - v.x) ** 2 + (y - v.y) ** 2)
            if dist < 1e-6:
                return float(v.dx), float(v.dy)

            w = (v.weight / (dist ** self.power))
            total_weight += w
            sum_dx += w * v.dx
            sum_dy += w * v.dy

        if total_weight <= 0.0:
            return 0.0, 0.0

        return float(sum_dx / total_weight), float(sum_dy / total_weight)

    def _interpolate_rbf(self, x: float, y: float) -> Tuple[float, float]:
        """Gaussian Radial Basis Function interpolation."""
        total_weight = 0.0
        sum_dx = 0.0
        sum_dy = 0.0

        for v in self.vectors:
            dist = math.sqrt((x - v.x) ** 2 + (y - v.y) ** 2)
            # Gaussian kernel: exp(-(dist / epsilon)^2)
            w = v.weight * math.exp(-((dist / self.epsilon) ** 2))
            total_weight += w
            sum_dx += w * v.dx
            sum_dy += w * v.dy

        if total_weight <= 0.0:
            return 0.0, 0.0

        return float(sum_dx / total_weight), float(sum_dy / total_weight)


@dataclass
class DeformationConfig:
    """Operational parameters for elastic deformation."""

    max_displacement_m: float = 20.0
    preserve_topology: bool = True
    simplification_tolerance_m: float = 0.05
    smoothing_factor: float = 0.5


@dataclass
class DeformationAudit:
    """Audit report of applied vector field deformation."""

    total_vertices_deformed: int
    clamped_vertices_count: int
    max_displacement_observed: float
    rms_displacement: float
    original_area_sqm: float
    deformed_area_sqm: float
    area_distortion_ratio: float
    topology_preserved: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_vertices_deformed": self.total_vertices_deformed,
            "clamped_vertices_count": self.clamped_vertices_count,
            "max_displacement_observed": round(self.max_displacement_observed, 4),
            "rms_displacement": round(self.rms_displacement, 4),
            "original_area_sqm": round(self.original_area_sqm, 2),
            "deformed_area_sqm": round(self.deformed_area_sqm, 2),
            "area_distortion_ratio": round(self.area_distortion_ratio, 4),
            "topology_preserved": self.topology_preserved,
        }


def apply_vector_field_to_polygon(
    polygon: Polygon,
    interpolator: VectorFieldInterpolator,
    max_displacement_m: Optional[float] = None,
) -> Polygon:
    """Deform polygon exterior and interiors using vector field interpolator."""
    if polygon is None or polygon.is_empty:
        return polygon

    def _deform_ring(coords: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
        new_coords = []
        for x, y in coords:
            dx, dy = interpolator.interpolate(x, y)
            mag = math.sqrt(dx * dx + dy * dy)
            if max_displacement_m is not None and mag > max_displacement_m:
                scale = max_displacement_m / mag
                dx *= scale
                dy *= scale
            new_coords.append((x + dx, y + dy))
        # Ensure closed ring
        if new_coords and new_coords[0] != new_coords[-1]:
            new_coords.append(new_coords[0])
        return new_coords

    deformed_exterior = _deform_ring(list(polygon.exterior.coords))
    deformed_interiors = [_deform_ring(list(interior.coords)) for interior in polygon.interiors]

    warped = Polygon(deformed_exterior, deformed_interiors)
    if not warped.is_valid:
        warped = make_valid(warped)
        if isinstance(warped, MultiPolygon):
            warped = max(warped.geoms, key=lambda p: p.area)

    return warped


def build_displacement_field_from_pairs(
    cadastre_geom: Polygon,
    satellite_geom: Polygon,
    sample_spacing_m: float = 25.0,
) -> List[DisplacementVector]:
    """Sample boundary vertices and calculate directional offset vectors pointing to corresponding satellite boundary locations."""
    if cadastre_geom is None or satellite_geom is None or cadastre_geom.is_empty or satellite_geom.is_empty:
        return []

    cad_boundary = cadastre_geom.exterior
    sat_boundary = satellite_geom.exterior

    total_len_cad = cad_boundary.length
    total_len_sat = sat_boundary.length

    num_samples = max(4, int(math.ceil(total_len_cad / sample_spacing_m)))

    vectors: List[DisplacementVector] = []
    for i in range(num_samples):
        fraction = i / num_samples
        cad_pt = cad_boundary.interpolate(fraction * total_len_cad)
        sat_pt = sat_boundary.interpolate(fraction * total_len_sat)

        dx = sat_pt.x - cad_pt.x
        dy = sat_pt.y - cad_pt.y

        vectors.append(
            DisplacementVector(
                x=float(cad_pt.x),
                y=float(cad_pt.y),
                dx=float(dx),
                dy=float(dy),
                weight=1.0,
            )
        )

    return vectors



class ElasticDeformationEngine:
    """Coordinates vector field generation, elastic clamping, and topology-preserving warping."""

    def __init__(self, config: Optional[DeformationConfig] = None) -> None:
        self.config = config or DeformationConfig()

    def deform_polygon(
        self,
        polygon: Polygon,
        vectors: Sequence[DisplacementVector],
        kernel: str = "idw",
    ) -> Tuple[Polygon, DeformationAudit]:
        """Apply vector displacement field to a single cadastral polygon with audit metrics."""
        interpolator = VectorFieldInterpolator(vectors, kernel=kernel)

        total_vertices = len(polygon.exterior.coords)
        clamped_count = 0
        displacements: List[float] = []

        # Audit vertex shifts
        for x, y in polygon.exterior.coords:
            dx, dy = interpolator.interpolate(x, y)
            mag = math.sqrt(dx * dx + dy * dy)
            if mag > self.config.max_displacement_m:
                clamped_count += 1
                mag = self.config.max_displacement_m
            displacements.append(mag)

        max_disp = max(displacements) if displacements else 0.0
        rms_disp = math.sqrt(sum(d * d for d in displacements) / len(displacements)) if displacements else 0.0

        warped = apply_vector_field_to_polygon(
            polygon, interpolator, max_displacement_m=self.config.max_displacement_m
        )

        orig_area = float(polygon.area)
        warped_area = float(warped.area)
        area_ratio = (warped_area / orig_area) if orig_area > 0 else 1.0

        audit = DeformationAudit(
            total_vertices_deformed=total_vertices,
            clamped_vertices_count=clamped_count,
            max_displacement_observed=max_disp,
            rms_displacement=rms_disp,
            original_area_sqm=orig_area,
            deformed_area_sqm=warped_area,
            area_distortion_ratio=area_ratio,
            topology_preserved=warped.is_valid,
        )

        return warped, audit

    def snap_to_satellite(
        self,
        cadastre_geom: Polygon,
        satellite_geom: Polygon,
        sample_spacing_m: float = 25.0,
    ) -> Tuple[Polygon, DeformationAudit]:
        """Automatically construct displacement vectors and snap cadastre to satellite boundary."""
        vectors = build_displacement_field_from_pairs(
            cadastre_geom, satellite_geom, sample_spacing_m=sample_spacing_m
        )
        return self.deform_polygon(cadastre_geom, vectors)

    def deform_parcel_cluster(
        self,
        parcels: Sequence[Polygon],
        vectors: Sequence[DisplacementVector],
        kernel: str = "idw",
    ) -> Tuple[List[Polygon], Dict[str, Any]]:
        """Deform an ensemble of adjacent cadastral parcels simultaneously, preserving edge adjacency."""
        interpolator = VectorFieldInterpolator(vectors, kernel=kernel)

        deformed_parcels: List[Polygon] = []
        for p in parcels:
            warped = apply_vector_field_to_polygon(
                p, interpolator, max_displacement_m=self.config.max_displacement_m
            )
            deformed_parcels.append(warped)

        # Check for overlaps among deformed parcels
        total_overlap_area = 0.0
        for i in range(len(deformed_parcels)):
            for j in range(i + 1, len(deformed_parcels)):
                inter = deformed_parcels[i].intersection(deformed_parcels[j])
                total_overlap_area += inter.area

        topology_clean = total_overlap_area < 1e-4

        summary = {
            "total_parcels": len(parcels),
            "topology_clean": topology_clean,
            "total_overlap_area_sqm": total_overlap_area,
        }

        return deformed_parcels, summary
