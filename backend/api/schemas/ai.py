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


class DisplacementVectorSchema(BaseModel):
    """Spatial displacement vector at a given coordinate."""

    x: float = Field(..., description="X coordinate of vector anchor")
    y: float = Field(..., description="Y coordinate of vector anchor")
    dx: float = Field(..., description="Displacement along X axis in coordinate units")
    dy: float = Field(..., description="Displacement along Y axis in coordinate units")
    weight: float = Field(1.0, gt=0.0, description="Confidence / weight of the displacement")
    magnitude: Optional[float] = Field(None, description="Euclidean displacement magnitude")


class DeformationConfigSchema(BaseModel):
    """Configuration parameters for elastic vector field interpolation and deformation."""

    interpolation_method: str = Field("GAUSSIAN_RBF", description="IDW or GAUSSIAN_RBF")
    max_displacement_m: float = Field(25.0, gt=0.0, description="Maximum permitted displacement before clamping")
    rbf_epsilon: float = Field(15.0, gt=0.0, description="RBF Gaussian bandwidth parameter")
    idw_power: float = Field(2.0, gt=0.0, description="Inverse distance weighting power")
    preserve_topology: bool = Field(True, description="Enforce validity and topology preservation")
    simplify_tolerance: float = Field(0.01, ge=0.0, description="Douglas-Peucker simplification tolerance")


class DeformationAuditSchema(BaseModel):
    """Audit report tracking structural and topological fidelity of a deformation."""

    total_vertices_deformed: int
    clamped_vertices_count: int
    max_displacement_observed: float
    rms_displacement: float
    original_area_sqm: float
    deformed_area_sqm: float
    area_distortion_ratio: float
    topology_preserved: bool


class DeformPolygonRequest(BaseModel):
    """Request to deform a parcel geometry according to supplied displacement vectors."""

    parcel_id: str = Field(..., min_length=1, description="Identifier of the land parcel")
    geojson: Dict[str, Any] = Field(..., description="GeoJSON polygon geometry")
    displacement_vectors: List[DisplacementVectorSchema] = Field(
        ..., min_length=1, description="List of discrete displacement vectors"
    )
    config: Optional[DeformationConfigSchema] = None


class DeformPolygonResponse(BaseModel):
    """Response containing deformed polygon geometry and deformation audit."""

    parcel_id: str
    deformed_geojson: Dict[str, Any]
    audit: DeformationAuditSchema


class SnapToSatelliteRequest(BaseModel):
    """Request to automatically extract boundary displacement vectors and snap cadastre to satellite."""

    parcel_id: str = Field(..., min_length=1, description="Identifier of the land parcel")
    cadastre_geojson: Dict[str, Any] = Field(..., description="Cadastral parcel polygon GeoJSON")
    satellite_geojson: Dict[str, Any] = Field(..., description="Detected satellite boundary polygon GeoJSON")
    num_boundary_samples: int = Field(50, ge=10, le=500, description="Number of boundary sample points")
    config: Optional[DeformationConfigSchema] = None


class SnapToSatelliteResponse(BaseModel):
    """Result of snapping cadastral boundary to satellite detection."""

    parcel_id: str
    deformed_geojson: Dict[str, Any]
    audit: DeformationAuditSchema
    generated_vectors_count: int


class ClusterParcelItem(BaseModel):
    """Parcel geometry item in a multi-parcel cluster."""

    parcel_id: str = Field(..., min_length=1)
    geojson: Dict[str, Any] = Field(..., description="GeoJSON polygon geometry")


class DeformedParcelItem(BaseModel):
    """Result item of deformed parcel in a cluster."""

    parcel_id: str
    deformed_geojson: Dict[str, Any]
    audit: DeformationAuditSchema


class DeformClusterRequest(BaseModel):
    """Request to deform an entire cluster of adjacent parcels under a unified displacement field."""

    parcels: List[ClusterParcelItem] = Field(..., min_length=1, description="List of parcels in cluster")
    displacement_vectors: List[DisplacementVectorSchema] = Field(
        ..., min_length=1, description="Cluster-wide displacement vectors"
    )
    config: Optional[DeformationConfigSchema] = None


class DeformClusterResponse(BaseModel):
    """Response containing deformed cluster parcels."""

    total_parcels: int
    results: List[DeformedParcelItem]

