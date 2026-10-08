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
