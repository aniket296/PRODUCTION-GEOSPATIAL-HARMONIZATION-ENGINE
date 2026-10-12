"""Satellite-cadastre boundary alignment scoring and discrepancy analysis engine.

Provides:
1. Intersection over Union (IoU / Jaccard Index) calculation.
2. Directed and undirected classical Hausdorff distance computation.
3. Modified Hausdorff Distance (MHD) for outlier-robust average displacement measurement.
4. Boundary F1-Score (Precision & Recall) using configurable spatial tolerance buffers.
5. Geometric discrepancies: centroid vector displacement (dx, dy) and area/perimeter divergence.
6. Composite alignment score (0 - 100) and classification (EXCELLENT, ACCEPTABLE, POOR, MISALIGNED).
7. Automatic alignment compensation hints (suggested translation vectors).
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
import numpy as np
from shapely.geometry import MultiPolygon, Polygon, base
from shapely.ops import nearest_points


class AlignmentCategory(str, enum.Enum):
    """Categorization of satellite-to-cadastre boundary conformance."""

    EXCELLENT = "EXCELLENT"       # IoU >= 0.90, Hausdorff <= 2m
    ACCEPTABLE = "ACCEPTABLE"     # IoU >= 0.75, Hausdorff <= 8m
    POOR = "POOR"                 # IoU >= 0.50, Hausdorff <= 20m
    MISALIGNED = "MISALIGNED"     # IoU < 0.50 or Hausdorff > 20m


@dataclass
class AlignmentHint:
    """Corrective spatial adjustment recommendation calculated from discrepancies."""

    suggested_dx: float
    suggested_dy: float
    confidence: float
    notes: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suggested_dx": round(self.suggested_dx, 4),
            "suggested_dy": round(self.suggested_dy, 4),
            "confidence": round(self.confidence, 4),
            "notes": self.notes,
        }


@dataclass
class BoundaryAlignmentMetrics:
    """Comprehensive boundary alignment assessment metrics between cadastre and satellite geometries."""

    parcel_id: str
    iou: float
    hausdorff_distance: float
    modified_hausdorff_distance: float
    boundary_precision: float
    boundary_recall: float
    boundary_f1: float
    centroid_displacement_x: float
    centroid_displacement_y: float
    centroid_displacement_distance: float
    area_cadastre_sqm: float
    area_satellite_sqm: float
    area_ratio: float
    perimeter_cadastre_m: float
    perimeter_satellite_m: float
    overall_score: float
    category: AlignmentCategory
    alignment_hint: Optional[AlignmentHint] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "iou": round(self.iou, 4),
            "hausdorff_distance": round(self.hausdorff_distance, 4),
            "modified_hausdorff_distance": round(self.modified_hausdorff_distance, 4),
            "boundary_precision": round(self.boundary_precision, 4),
            "boundary_recall": round(self.boundary_recall, 4),
            "boundary_f1": round(self.boundary_f1, 4),
            "centroid_displacement_x": round(self.centroid_displacement_x, 4),
            "centroid_displacement_y": round(self.centroid_displacement_y, 4),
            "centroid_displacement_distance": round(self.centroid_displacement_distance, 4),
            "area_cadastre_sqm": round(self.area_cadastre_sqm, 2),
            "area_satellite_sqm": round(self.area_satellite_sqm, 2),
            "area_ratio": round(self.area_ratio, 4),
            "perimeter_cadastre_m": round(self.perimeter_cadastre_m, 2),
            "perimeter_satellite_m": round(self.perimeter_satellite_m, 2),
            "overall_score": round(self.overall_score, 2),
            "category": self.category.value,
            "alignment_hint": self.alignment_hint.to_dict() if self.alignment_hint else None,
        }


def compute_iou(geom_a: base.BaseGeometry, geom_b: base.BaseGeometry) -> float:
    """Compute Intersection over Union (IoU / Jaccard Index) between two geometries."""
    if geom_a is None or geom_b is None or geom_a.is_empty or geom_b.is_empty:
        return 0.0

    try:
        intersection_area = geom_a.intersection(geom_b).area
        union_area = geom_a.union(geom_b).area
        if union_area <= 0.0:
            return 0.0
        return float(intersection_area / union_area)
    except Exception:
        return 0.0


def compute_hausdorff_distance(geom_a: base.BaseGeometry, geom_b: base.BaseGeometry) -> float:
    """Compute classical undirected Hausdorff distance between two geometries."""
    if geom_a is None or geom_b is None or geom_a.is_empty or geom_b.is_empty:
        return float("inf")

    try:
        # shapely.hausdorff_distance evaluates boundary distances
        return float(geom_a.hausdorff_distance(geom_b))
    except Exception:
        return float("inf")


def compute_modified_hausdorff_distance(
    geom_a: Polygon, geom_b: Polygon, num_samples: int = 100
) -> float:
    """Compute Modified Hausdorff Distance (MHD) using mean of minimum distances."""
    if geom_a is None or geom_b is None or geom_a.is_empty or geom_b.is_empty:
        return float("inf")

    try:
        boundary_a = geom_a.exterior
        boundary_b = geom_b.exterior

        len_a = boundary_a.length
        len_b = boundary_b.length

        if len_a <= 0 or len_b <= 0:
            return 0.0

        pts_a = [boundary_a.interpolate(i * len_a / num_samples) for i in range(num_samples)]
        pts_b = [boundary_b.interpolate(j * len_b / num_samples) for j in range(num_samples)]

        # d(A, B) = mean_{a in A} min_{b in B} ||a - b||
        d_ab = float(np.mean([pt.distance(boundary_b) for pt in pts_a]))
        # d(B, A) = mean_{b in B} min_{a in A} ||b - a||
        d_ba = float(np.mean([pt.distance(boundary_a) for pt in pts_b]))

        return max(d_ab, d_ba)
    except Exception:
        return float("inf")


def compute_boundary_f1(
    cadastre_geom: Polygon, satellite_geom: Polygon, buffer_tolerance_m: float = 2.0
) -> Tuple[float, float, float]:
    """Compute boundary precision, recall, and F1-score within a buffer tolerance.

    - Precision: Proportion of satellite boundary falling within buffer of cadastre boundary.
    - Recall: Proportion of cadastre boundary falling within buffer of satellite boundary.
    """
    if (
        cadastre_geom is None
        or satellite_geom is None
        or cadastre_geom.is_empty
        or satellite_geom.is_empty
    ):
        return 0.0, 0.0, 0.0

    try:
        cad_boundary = cadastre_geom.boundary
        sat_boundary = satellite_geom.boundary

        if cad_boundary.length <= 0 or sat_boundary.length <= 0:
            return 0.0, 0.0, 0.0

        cad_buffer = cad_boundary.buffer(buffer_tolerance_m)
        sat_buffer = sat_boundary.buffer(buffer_tolerance_m)

        # Matched lengths
        matched_sat = sat_boundary.intersection(cad_buffer).length
        matched_cad = cad_boundary.intersection(sat_buffer).length

        precision = min(1.0, max(0.0, matched_sat / sat_boundary.length))
        recall = min(1.0, max(0.0, matched_cad / cad_boundary.length))

        if precision + recall <= 0:
            f1 = 0.0
        else:
            f1 = 2.0 * (precision * recall) / (precision + recall)

        return float(f1), float(precision), float(recall)
    except Exception:
        return 0.0, 0.0, 0.0


def compute_centroid_displacement(
    cadastre_geom: base.BaseGeometry, satellite_geom: base.BaseGeometry
) -> Tuple[float, float, float]:
    """Compute translation vector (dx, dy) and Euclidean displacement distance between centroids.

    Returns: (dx, dy, distance) where dx = sat_x - cad_x, dy = sat_y - cad_y.
    """
    if (
        cadastre_geom is None
        or satellite_geom is None
        or cadastre_geom.is_empty
        or satellite_geom.is_empty
    ):
        return 0.0, 0.0, 0.0

    c_cad = cadastre_geom.centroid
    c_sat = satellite_geom.centroid

    dx = c_sat.x - c_cad.x
    dy = c_sat.y - c_cad.y
    distance = math.sqrt(dx * dx + dy * dy)

    return float(dx), float(dy), float(distance)


class BoundaryAlignmentScorer:
    """Scores geospatial alignment between cadastral land records and satellite boundaries."""

    def __init__(
        self,
        tolerance_buffer_m: float = 2.0,
        mhd_samples: int = 80,
    ) -> None:
        self.tolerance_buffer_m = tolerance_buffer_m
        self.mhd_samples = mhd_samples

    def evaluate(
        self,
        cadastre_geom: Polygon,
        satellite_geom: Polygon,
        parcel_id: str = "UNKNOWN",
    ) -> BoundaryAlignmentMetrics:
        """Evaluate alignment metrics for a single cadastre-satellite parcel pair."""
        iou = compute_iou(cadastre_geom, satellite_geom)
        hd = compute_hausdorff_distance(cadastre_geom, satellite_geom)
        mhd = compute_modified_hausdorff_distance(
            cadastre_geom, satellite_geom, num_samples=self.mhd_samples
        )
        f1, prec, rec = compute_boundary_f1(
            cadastre_geom, satellite_geom, buffer_tolerance_m=self.tolerance_buffer_m
        )
        dx, dy, dist = compute_centroid_displacement(cadastre_geom, satellite_geom)

        area_cad = float(cadastre_geom.area)
        area_sat = float(satellite_geom.area)
        area_ratio = float(area_sat / area_cad) if area_cad > 0 else 0.0

        perim_cad = float(cadastre_geom.length)
        perim_sat = float(satellite_geom.length)

        # Composite score calculation (0 - 100):
        # 40% weight on IoU (0 to 1 -> 0 to 40)
        # 30% weight on Boundary F1 (0 to 1 -> 0 to 30)
        # 20% weight on Hausdorff penalty (diminishes above tolerance)
        # 10% weight on Area Ratio proximity to 1.0
        iou_score = iou * 40.0
        f1_score = f1 * 30.0

        # Hausdorff score: full 20 pts if hd <= buffer, decaying to 0 at 25m
        hd_score = max(0.0, min(20.0, 20.0 * (1.0 - max(0.0, hd - self.tolerance_buffer_m) / 25.0)))

        # Area ratio score: full 10 pts if ratio == 1.0, decaying with absolute divergence
        area_divergence = abs(1.0 - area_ratio)
        area_score = max(0.0, min(10.0, 10.0 * (1.0 - area_divergence)))

        overall_score = float(iou_score + f1_score + hd_score + area_score)

        # Categorization based on composite indicators
        if iou >= 0.90 and hd <= self.tolerance_buffer_m * 1.5:
            category = AlignmentCategory.EXCELLENT
        elif iou >= 0.70 and hd <= 12.0:
            category = AlignmentCategory.ACCEPTABLE
        elif iou >= 0.40 and hd <= 25.0:
            category = AlignmentCategory.POOR
        else:
            category = AlignmentCategory.MISALIGNED

        # Alignment hint computation (suggested translation to bring satellite back onto cadastre)
        hint = None
        if dist > 0.1:
            hint = AlignmentHint(
                suggested_dx=-dx,
                suggested_dy=-dy,
                confidence=min(1.0, max(0.2, 1.0 - (hd / 50.0))),
                notes=f"Centroid displacement of {dist:.2f}m detected (dx={dx:.2f}m, dy={dy:.2f}m).",
            )

        return BoundaryAlignmentMetrics(
            parcel_id=parcel_id,
            iou=iou,
            hausdorff_distance=hd,
            modified_hausdorff_distance=mhd,
            boundary_precision=prec,
            boundary_recall=rec,
            boundary_f1=f1,
            centroid_displacement_x=dx,
            centroid_displacement_y=dy,
            centroid_displacement_distance=dist,
            area_cadastre_sqm=area_cad,
            area_satellite_sqm=area_sat,
            area_ratio=area_ratio,
            perimeter_cadastre_m=perim_cad,
            perimeter_satellite_m=perim_sat,
            overall_score=overall_score,
            category=category,
            alignment_hint=hint,
        )

    def evaluate_batch(
        self, parcel_pairs: Sequence[Tuple[str, Polygon, Polygon]]
    ) -> List[BoundaryAlignmentMetrics]:
        """Evaluate a batch of (parcel_id, cadastre_polygon, satellite_polygon) triplets."""
        results = []
        for pid, cad_geom, sat_geom in parcel_pairs:
            results.append(self.evaluate(cad_geom, sat_geom, parcel_id=pid))
        return results
