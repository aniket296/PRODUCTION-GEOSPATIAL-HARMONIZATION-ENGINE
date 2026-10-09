"""Geodetic and Coordinate Reference System (CRS) API schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class CRSSummaryResponse(BaseModel):
    """Condensed summary schema for CRS listing and catalog search."""

    srid: str = Field(..., description="Canonical SRID string (e.g. 'EPSG:4326')")
    auth_name: str = Field(..., description="Authority name (e.g. 'EPSG', 'LOCAL')")
    auth_code: str = Field(..., description="Authority code")
    epsg_code: Optional[int] = Field(None, description="EPSG numeric code if applicable")
    name: str = Field(..., description="Human-readable CRS name")
    proj_type: str = Field(..., description="Projection type (GEOGRAPHIC_2D, PROJECTED, LOCAL)")
    unit: str = Field(..., description="Unit of measurement (degree, metre)")
    area_of_use_name: str = Field(..., description="Geographic domain of applicability")
    area_sq_deg: float = Field(..., description="Approximate domain extent in sq degrees")


class CRSDetailResponse(CRSSummaryResponse):
    """Full detail schema for CRS definitions including geodetic ellipsoid and parameters."""

    datum_name: str = Field(..., description="Geodetic datum name")
    ellipsoid: Dict[str, Any] = Field(..., description="Ellipsoid geometric parameters (a, b, rf)")
    bounds: List[float] = Field(..., description="Geographic domain bounding box [W, S, E, N]")
    aliases: List[str] = Field(default_factory=list, description="Registered aliases and historical names")
    wkt2: Optional[str] = Field(None, description="WKT2:2019 string representation")
    proj4: Optional[str] = Field(None, description="PROJ4 string representation")


class CRSValidationRequest(BaseModel):
    """Request payload to validate coordinates against a CRS geographic domain."""

    identifier: str = Field(..., description="CRS identifier (e.g. 'EPSG:32643', 'UTM_43N')")
    coordinates: List[float] = Field(
        ...,
        description="Either [lon, lat] point or [min_lon, min_lat, max_lon, max_lat] bounding box",
    )


class CRSValidationResponse(BaseModel):
    """Response containing CRS validity status and domain conformance."""

    is_valid_syntax: bool = Field(..., description="Whether identifier resolved to a recognized CRS")
    in_bounds: bool = Field(..., description="Whether coordinates fall within the CRS geographic validity domain")
    identifier: str = Field(..., description="Input identifier")
    resolved_srid: Optional[str] = Field(None, description="Canonical resolved SRID")
    error_detail: Optional[str] = Field(None, description="Error message if validation failed")


class CRSSuggestionRequest(BaseModel):
    """Request to determine optimal CRS recommendations for coordinate or cadastral attributes."""

    longitude: float = Field(..., description="Centroid longitude in decimal degrees (WGS 84)")
    latitude: float = Field(..., description="Centroid latitude in decimal degrees (WGS 84)")
    state: Optional[str] = Field(None, description="Optional Indian State name or 2-letter code")
    toposheet: Optional[str] = Field(None, description="Optional Survey of India toposheet ID")


class CRSSuggestionResponse(BaseModel):
    """Automated CRS recommendations for Indian cadastral harmonization."""

    centroid: List[float] = Field(..., description="[longitude, latitude]")
    suggested_utm_epsg: int = Field(..., description="Recommended WGS 84 UTM zone EPSG code")
    suggested_utm_srid: str = Field(..., description="Formatted UTM SRID")
    suggested_soi_epsg: Optional[int] = Field(None, description="Recommended Kalianpur 1975 zone EPSG code")
    suggested_soi_srid: Optional[str] = Field(None, description="Formatted Kalianpur SRID")
    state_name: Optional[str] = Field(None, description="Matched Indian state name if applicable")


class LocalCadastralGridRequest(BaseModel):
    """Payload to register a custom local village cadastral survey engineering frame."""

    grid_id: str = Field(..., description="Unique alphanumeric identifier for local grid")
    name: str = Field(..., description="Descriptive name (e.g. 'Village Rampur Revenue Grid')")
    village_name: str = Field(..., description="Village / Mouza name")
    district: str = Field(..., description="Revenue district")
    state: str = Field(..., description="State name")
    origin_lon: float = Field(..., description="Local origin longitude (WGS 84)")
    origin_lat: float = Field(..., description="Local origin latitude (WGS 84)")
    false_easting: float = Field(0.0, description="False easting at origin in metres")
    false_northing: float = Field(0.0, description="False northing at origin in metres")
    scale_factor: float = Field(1.0, description="Combined ground-to-grid scale factor (k0)")
    azimuth_rotation_deg: float = Field(0.0, description="Grid azimuth rotation in degrees")
    extent_radius_km: float = Field(25.0, description="Valid operational radius in kilometres")


class ToposheetResponse(BaseModel):
    """Survey of India toposheet parsing result."""

    sheet_id: str = Field(..., description="Normalized Survey of India sheet number")
    scale: str = Field(..., description="Map series scale (1:1M, 1:250k, 1:50k)")
    bounds: List[float] = Field(..., description="Geographic bounding box [W, S, E, N]")
    centroid: List[float] = Field(..., description="Centroid [longitude, latitude]")
    suggested_utm_epsg: int = Field(..., description="Recommended UTM zone EPSG code")
    suggested_soi_epsg: Optional[int] = Field(None, description="Recommended Kalianpur 1975 zone EPSG code")


class CoordinateTransformPointRequest(BaseModel):
    """Request to transform a single coordinate point between datums."""

    source_srid: str = Field(..., description="Source coordinate reference system SRID (e.g. 'EPSG:4240')")
    target_srid: str = Field(..., description="Target coordinate reference system SRID (e.g. 'EPSG:4326')")
    coordinates: List[float] = Field(..., description="[longitude, latitude] or [longitude, latitude, height]")


class CoordinateTransformPointResponse(BaseModel):
    """Response containing datum-transformed point coordinates."""

    source_srid: str
    target_srid: str
    transformed_coordinates: List[float]


class CoordinateTransformBatchRequest(BaseModel):
    """Request to batch-transform multiple coordinates between datums."""

    source_srid: str = Field(..., description="Source coordinate reference system SRID")
    target_srid: str = Field(..., description="Target coordinate reference system SRID")
    coordinates: List[List[float]] = Field(..., description="List of [lon, lat] or [lon, lat, h] coordinates")


class CoordinateTransformBatchResponse(BaseModel):
    """Response containing batch-transformed coordinates."""

    source_srid: str
    target_srid: str
    transformed_coordinates: List[List[float]]
    count: int


class HelmertFitGCPPair(BaseModel):
    """3D Ground Control Point pair for Helmert parameter adjustment."""

    source_xyz: List[float] = Field(..., description="Source 3D Cartesian coordinates [X, Y, Z] in metres")
    target_xyz: List[float] = Field(..., description="Target 3D Cartesian coordinates [X, Y, Z] in metres")


class HelmertFitRequest(BaseModel):
    """Request to compute least-squares 7-parameter Helmert transformation from GCP pairs."""

    gcps: List[HelmertFitGCPPair] = Field(..., min_length=3, description="List of at least 3 GCP pairs")
    convention: str = Field("POSITION_VECTOR", description="Rotation sign convention ('POSITION_VECTOR' or 'COORDINATE_FRAME')")
    is_molodensky_badekas: bool = Field(False, description="Whether to compute evaluation centroid (10-parameter model)")


class HelmertFitResponse(BaseModel):
    """Statistical estimation results and Helmert parameters."""

    parameters: Dict[str, Any] = Field(..., description="Estimated 7-parameter Helmert values (dx, dy, dz, rx, ry, rz, scale_ppm)")
    rmse_3d: float = Field(..., description="3D Root Mean Square Error in metres")
    rmse_x: float = Field(..., description="X-axis Root Mean Square Error in metres")
    rmse_y: float = Field(..., description="Y-axis Root Mean Square Error in metres")
    rmse_z: float = Field(..., description="Z-axis Root Mean Square Error in metres")
    num_points: int = Field(..., description="Number of GCP point pairs evaluated")
    degrees_of_freedom: int = Field(..., description="Degrees of freedom (3N - 7)")
    sigma_0: float = Field(..., description="Standard error of unit weight")
    residuals: List[List[float]] = Field(..., description="Residual vector [vx, vy, vz] per GCP in metres")


class NormalizationConfigSchema(BaseModel):
    """Configuration options for polygon boundary sanitization."""

    snap_precision_m: float = Field(0.001, description="Grid snapping precision in metres (1 mm)")
    collinear_tolerance_m: float = Field(0.005, description="Collinear vertex decimation tolerance in metres (5 mm)")
    min_parcel_area_sqm: float = Field(1.0, description="Minimum allowable parcel area in square metres")
    sliver_compactness_threshold: float = Field(0.05, description="Polsby-Popper threshold for sliver detection")
    max_area_drift_pct: float = Field(0.05, description="Maximum allowable area change percentage (0.05%)")
    max_hausdorff_drift_m: float = Field(0.05, description="Maximum Hausdorff boundary displacement allowed in metres (5 cm)")
    auto_resolve_overlap_max_sqm: float = Field(5.0, description="Max overlap area eligible for automatic reconciliation")
    shared_boundary_snap_tolerance_m: float = Field(0.05, description="Distance tolerance to snap adjacent parcel edges")
    orient_ccw: bool = Field(True, description="Enforce exterior CCW and interior CW ring orientation")
    force_2d: bool = Field(True, description="Strip Z/M elevation ordinates to pure planar coordinates")
    repair_self_intersections: bool = Field(True, description="Resolve self-intersecting bow-ties and invalid rings")


class NormalizationAuditSchema(BaseModel):
    """Detailed audit metrics from polygon normalization."""

    original_area_sqm: float
    normalized_area_sqm: float
    area_drift_pct: float
    original_vertex_count: int
    normalized_vertex_count: int
    vertices_removed: int
    max_displacement_m: float
    repaired_defects: List[str]
    is_valid_polygon: bool
    is_multipolygon: bool
    num_parts: int
    num_holes: int


class NormalizeGeometryRequest(BaseModel):
    """Request payload to sanitize and normalize a cadastral parcel geometry."""

    geometry: Dict[str, Any] = Field(..., description="GeoJSON geometry object (Polygon or MultiPolygon)")
    config: Optional[NormalizationConfigSchema] = Field(None, description="Optional normalization configuration parameters")


class NormalizeGeometryResponse(BaseModel):
    """Response containing sanitized GeoJSON geometry and full normalization audit."""

    success: bool = True
    normalized_geometry: Dict[str, Any]
    audit: NormalizationAuditSchema


class ParcelGeometryItem(BaseModel):
    """Item representing a single cadastral parcel with identifier and GeoJSON geometry."""

    parcel_identifier: str = Field(..., description="Unique parcel identifier (e.g. 'KHASRA-101')")
    geometry: Dict[str, Any] = Field(..., description="GeoJSON geometry dictionary")
    khasra_number: Optional[str] = Field(None, description="Optional Khasra survey number")


class TopologicalAnomalySchema(BaseModel):
    """Individual detected topological anomaly."""

    defect_type: str
    severity: str
    description: str
    affected_parcel_ids: List[str]
    affected_area_sqm: float
    location: Optional[List[float]] = None
    geometry: Optional[Dict[str, Any]] = None


class CadastralTopologyReportSchema(BaseModel):
    """Topological health summary report for a cadastral parcel batch."""

    total_parcels: int
    valid_parcels_count: int
    invalid_parcels_count: int
    is_planar_graph_valid: bool
    total_overlap_area_sqm: float
    total_gap_area_sqm: float
    auto_resolvable_count: int
    critical_dispute_count: int
    anomalies_count: int
    anomalies: List[TopologicalAnomalySchema]


class ValidateTopologyBatchRequest(BaseModel):
    """Request payload to validate planar topology across a batch of cadastral parcels."""

    parcels: List[ParcelGeometryItem] = Field(..., min_length=1, description="List of cadastral parcels to validate")
    config: Optional[NormalizationConfigSchema] = Field(None, description="Optional topology configuration parameters")


class ValidateTopologyBatchResponse(BaseModel):
    """Response containing comprehensive topological anomaly report."""

    success: bool = True
    report: CadastralTopologyReportSchema


class SnapSharedBoundariesRequest(BaseModel):
    """Request payload to snap shared boundaries between adjacent parcels."""

    parcels: List[ParcelGeometryItem] = Field(..., min_length=2, description="List of parcels to align")
    tolerance_m: float = Field(0.05, description="Snapping distance tolerance in metres (e.g. 0.05m = 5cm)")


class SnapSharedBoundariesResponse(BaseModel):
    """Response containing aligned parcels."""

    success: bool = True
    aligned_parcels: List[ParcelGeometryItem]
    count: int


