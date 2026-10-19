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


class SuperResolutionConfigSchema(BaseModel):
    """Configuration for satellite raster super-resolution and edge enhancement."""

    scale_factor: int = Field(2, ge=1, le=8, description="Spatial upsampling scale factor")
    sharpen_amount: float = Field(1.2, ge=0.0, le=5.0, description="Unsharp masking strength")
    sharpen_radius: float = Field(1.0, gt=0.0, le=10.0, description="Gaussian blur sigma for unsharp mask")
    apply_contrast_enhancement: bool = Field(True, description="Apply percentile contrast stretching")
    lower_percentile: float = Field(2.0, ge=0.0, lt=50.0, description="Lower clipping percentile")
    upper_percentile: float = Field(98.0, gt=50.0, le=100.0, description="Upper clipping percentile")
    original_gsd_m: float = Field(10.0, gt=0.0, description="Original Ground Sampling Distance in meters")
    edge_threshold: float = Field(0.15, ge=0.0, le=1.0, description="Normalized Sobel edge detection threshold")


class SuperResolutionAuditSchema(BaseModel):
    """Audit metrics quantifying spatial resolution and edge sharpness gains."""

    original_shape: List[int]
    enhanced_shape: List[int]
    scale_factor: int
    original_gsd_m: float
    enhanced_gsd_m: float
    initial_sharpness: float
    enhanced_sharpness: float
    sharpness_gain_ratio: float
    mean_radiance_before: float
    mean_radiance_after: float


class SuperResolutionEnhanceRequest(BaseModel):
    """Request to run super-resolution, contrast stretching, and edge sharpening on a raster patch."""

    tile_id: str = Field(..., min_length=1, description="Identifier of the satellite raster tile")
    raster_matrix: List[List[Any]] = Field(
        ..., min_length=2, description="2D (H, W) or 3D (H, W, C) raster reflectance array"
    )
    config: Optional[SuperResolutionConfigSchema] = None


class SuperResolutionEnhanceResponse(BaseModel):
    """Response containing super-resolved raster patch and sharpness audit."""

    tile_id: str
    enhanced_matrix: List[List[Any]]
    audit: SuperResolutionAuditSchema


class BoundaryMaskExtractRequest(BaseModel):
    """Request to enhance a low-res satellite tile and extract binary parcel boundary edge mask."""

    tile_id: str = Field(..., min_length=1, description="Identifier of the satellite raster tile")
    raster_matrix: List[List[Any]] = Field(
        ..., min_length=2, description="2D (H, W) or 3D (H, W, C) raster reflectance array"
    )
    config: Optional[SuperResolutionConfigSchema] = None


class BoundaryMaskExtractResponse(BaseModel):
    """Response containing extracted binary boundary edge mask at super-resolved GSD."""

    tile_id: str
    boundary_mask: List[List[bool]]
    edge_pixel_count: int
    edge_density_ratio: float
    enhanced_gsd_m: float


class VectorizationConfigSchema(BaseModel):
    """Configuration for automated raster-to-vector polygon extraction."""

    origin_x: float = Field(0.0, description="Geographic X coordinate of raster top-left origin")
    origin_y: float = Field(0.0, description="Geographic Y coordinate of raster top-left origin")
    gsd_m: float = Field(1.0, gt=0.0, description="Ground Sampling Distance per pixel in meters")
    simplify_tolerance_m: float = Field(1.0, ge=0.0, description="Douglas-Peucker simplification tolerance in meters")
    min_area_sqm: float = Field(25.0, gt=0.0, description="Minimum parcel area in square meters")
    max_area_sqm: Optional[float] = Field(None, gt=0.0, description="Maximum parcel area in square meters")
    preserve_topology: bool = Field(True, description="Preserve polygon topology during simplification")
    connectivity: int = Field(4, description="Connected-component connectivity (4 or 8)")


class VectorizedParcelSchema(BaseModel):
    """Vectorized parcel geometry and metadata."""

    parcel_id: str
    geojson: Dict[str, Any]
    area_sqm: float
    perimeter_m: float
    vertex_count_raw: int
    vertex_count_simplified: int
    pixel_count: int


class VectorizationAuditSchema(BaseModel):
    """Audit metrics for a raster-to-vector polygon extraction operation."""

    total_parcels_extracted: int
    noise_components_filtered: int
    oversized_components_filtered: int
    total_area_sqm: float
    all_geometries_valid: bool


class VectorizeMaskRequest(BaseModel):
    """Request to vectorize a binary foreground or boundary edge mask into simplified GeoJSON polygons."""

    tile_id: str = Field(..., min_length=1, description="Identifier of the raster tile")
    binary_mask: List[List[bool]] = Field(..., min_length=2, description="2D boolean mask (H, W)")
    mask_type: str = Field("FOREGROUND", description="FOREGROUND (True = parcel interior) or EDGE (True = bund wall)")
    config: Optional[VectorizationConfigSchema] = None


class VectorizeMaskResponse(BaseModel):
    """Response containing extracted GeoJSON parcel polygons and vectorization audit."""

    tile_id: str
    parcels: List[VectorizedParcelSchema]
    audit: VectorizationAuditSchema




class ChangeDetectionConfigSchema(BaseModel):
    """Configuration for multi-temporal change detection and encroachment flagging."""

    origin_x: float = Field(0.0, description="Geographic X coordinate of raster top-left origin")
    origin_y: float = Field(0.0, description="Geographic Y coordinate of raster top-left origin")
    gsd_m: float = Field(10.0, gt=0.0, description="Ground Sampling Distance per pixel in meters")
    change_magnitude_threshold: float = Field(0.25, gt=0.0, description="Change Vector Analysis magnitude threshold")
    buffer_tolerance_m: float = Field(0.5, ge=0.0, description="Boundary registration tolerance in meters")
    min_encroachment_sqm: float = Field(5.0, ge=0.0, description="Minimum spillover area to flag as encroachment")
    min_footprint_sqm: float = Field(4.0, gt=0.0, description="Minimum new built-up footprint area in square meters")


class TemporalChangeSchema(BaseModel):
    """Summary statistics of multi-temporal raster differencing."""

    changed_pixel_count: int
    changed_fraction: float
    builtup_pixel_count: int
    mean_ndvi_delta: float
    mean_ndbi_delta: float


class EncroachmentResultSchema(BaseModel):
    """Geometric encroachment assessment for a parcel."""

    parcel_id: str
    has_encroachment: bool
    severity: str = Field(..., description="NONE, MINOR, MODERATE or CRITICAL")
    encroached_area_sqm: float
    encroachment_percentage: float
    max_lateral_intrusion_m: float
    public_corridor_intrusion_sqm: float
    affected_corridor_ids: List[str]
    encroached_geojson: Optional[Dict[str, Any]] = None


class ChangeDetectionAnalyzeRequest(BaseModel):
    """Request to analyze a T1/T2 raster pair against a legal cadastral parcel."""

    parcel_id: str = Field(..., min_length=1, description="Identifier of the land parcel")
    cadastre_geojson: Dict[str, Any] = Field(..., description="GeoJSON polygon of the legal parcel")
    t1_raster: List[List[List[float]]] = Field(..., min_length=2, description="Baseline (H, W, 3) raster: Red, NIR, SWIR")
    t2_raster: List[List[List[float]]] = Field(..., min_length=2, description="Current (H, W, 3) raster: Red, NIR, SWIR")
    public_corridors: Optional[Dict[str, Dict[str, Any]]] = Field(
        None, description="Map of corridor id to GeoJSON polygon (roads, canals, rights-of-way)"
    )
    config: Optional[ChangeDetectionConfigSchema] = None


class ChangeDetectionAnalyzeResponse(BaseModel):
    """Per-parcel change detection and encroachment report."""

    parcel_id: str
    temporal_change: TemporalChangeSchema
    encroachment: EncroachmentResultSchema
    new_footprints: List[Dict[str, Any]]


class EncroachmentEvaluateRequest(BaseModel):
    """Request to evaluate geometric encroachment of an occupied footprint on a legal parcel."""

    parcel_id: str = Field(..., min_length=1, description="Identifier of the land parcel")
    cadastre_geojson: Dict[str, Any] = Field(..., description="GeoJSON polygon of the legal parcel")
    occupied_geojson: Dict[str, Any] = Field(..., description="GeoJSON geometry of the occupied footprint")
    public_corridors: Optional[Dict[str, Dict[str, Any]]] = None
    buffer_tolerance_m: float = Field(0.5, ge=0.0, description="Boundary tolerance in meters")
    min_encroachment_sqm: float = Field(5.0, ge=0.0, description="Minimum spillover area to flag")
