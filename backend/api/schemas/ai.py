"""AI and boundary alignment schemas for computer vision harmonization."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AlignmentHintSchema(BaseModel):
    """Suggested spatial offset compensation."""

    suggested_dx: float = Field(..., description="Suggested X translation in coordinate units")
    suggested_dy: float = Field(..., description="Suggested Y translation in coordinate units")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in alignment recommendation")
    notes: str = Field(..., description="Descriptive diagnostic details")


class BoundaryAlignmentRequest(BaseModel):
    """Request payload to score alignment between cadastral and satellite parcel boundaries."""

    parcel_id: str = Field(..., min_length=1, description="Identifier of the land parcel")
    cadastre_geojson: Dict[str, Any] = Field(..., description="GeoJSON geometry of the cadastre parcel")
    satellite_geojson: Dict[str, Any] = Field(..., description="GeoJSON geometry extracted from satellite imagery")
    tolerance_buffer_m: float = Field(2.0, gt=0.0, description="Spatial tolerance buffer in meters")


class BoundaryAlignmentResponse(BaseModel):
    """Comprehensive boundary alignment assessment metrics."""

    parcel_id: str
    iou: float = Field(..., ge=0.0, le=1.0, description="Intersection over Union / Jaccard Index")
    hausdorff_distance: float = Field(..., ge=0.0, description="Maximum Hausdorff boundary distance in meters")
    modified_hausdorff_distance: float = Field(..., ge=0.0, description="Modified Hausdorff Distance (MHD) in meters")
    boundary_precision: float = Field(..., ge=0.0, le=1.0, description="Boundary coincidence precision")
    boundary_recall: float = Field(..., ge=0.0, le=1.0, description="Boundary coincidence recall")
    boundary_f1: float = Field(..., ge=0.0, le=1.0, description="Boundary coincidence F1-score")
    centroid_displacement_x: float = Field(..., description="Delta X displacement between centroids")
    centroid_displacement_y: float = Field(..., description="Delta Y displacement between centroids")
    centroid_displacement_distance: float = Field(..., ge=0.0, description="Euclidean centroid distance in meters")
    area_cadastre_sqm: float = Field(..., ge=0.0, description="Area of cadastral parcel in square meters")
    area_satellite_sqm: float = Field(..., ge=0.0, description="Area of satellite parcel in square meters")
    area_ratio: float = Field(..., ge=0.0, description="Ratio of satellite to cadastre area")
    perimeter_cadastre_m: float = Field(..., ge=0.0, description="Cadastral perimeter in meters")
    perimeter_satellite_m: float = Field(..., ge=0.0, description="Satellite perimeter in meters")
    overall_score: float = Field(..., ge=0.0, le=100.0, description="Composite alignment score (0 - 100)")
    category: str = Field(..., description="Alignment rating: EXCELLENT, ACCEPTABLE, POOR, MISALIGNED")
    alignment_hint: Optional[AlignmentHintSchema] = None


class BatchBoundaryAlignmentRequest(BaseModel):
    """Batch evaluation payload for multiple cadastral parcels."""

    items: List[BoundaryAlignmentRequest] = Field(..., min_length=1, description="List of parcel comparison requests")


class BatchBoundaryAlignmentResponse(BaseModel):
    """Batch alignment results summary."""

    total_evaluated: int
    excellent_count: int
    acceptable_count: int
    poor_count: int
    misaligned_count: int
    average_iou: float
    average_score: float
    results: List[BoundaryAlignmentResponse]
