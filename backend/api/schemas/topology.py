"""Pydantic schemas and RFC 7946 GeoJSON builders for topology error reporting."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from typing import Any, Dict, List, Literal, Optional

from geoalchemy2.shape import to_shape
from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import shape as shapely_shape

from backend.models.topology import TopologicalError, TopologySeverity


class TopologyErrorProperties(BaseModel):
    """GeoJSON Feature properties for a single `TopologicalError` record."""

    model_config = ConfigDict(from_attributes=True)

    layer_id: Optional[str] = Field(default=None, description="UUID of the parent cadastral layer")
    primary_parcel_id: Optional[str] = Field(default=None, description="Primary parcel UUID involved in conflict")
    secondary_parcel_id: Optional[str] = Field(
        default=None,
        description="Secondary parcel UUID involved in pairwise conflict",
    )
    error_type: str = Field(description="Anomaly category (OVERLAP, SLIVER, GAP, DANGLING_NODE, DUPLICATE_VERTEX)")
    severity: str = Field(description="Severity classification (AUTO_RESOLVABLE, CRITICAL_DISPUTE, WARNING, INFO)")
    description: Optional[str] = Field(default=None, description="Human-readable diagnostic description")
    overlap_percentage_primary: Optional[float] = Field(
        default=None,
        description="Overlap percentage relative to primary parcel area",
    )
    overlap_percentage_secondary: Optional[float] = Field(
        default=None,
        description="Overlap percentage relative to secondary parcel area",
    )
    affected_area_sqm: float = Field(default=0.0, ge=0.0, description="Affected metric area in square meters")
    is_resolved: bool = Field(default=False, description="Whether this conflict has been resolved")
    resolution_method: Optional[str] = Field(default=None, description="Algorithm or manual action used to resolve")
    resolved_at: Optional[str] = Field(default=None, description="ISO-8601 resolution timestamp")
    resolved_by: Optional[str] = Field(default=None, description="Actor or subsystem that resolved the conflict")
    resolution_details: Optional[Dict[str, Any]] = Field(default=None, description="Provenance audit metadata")


class TopologyErrorFeature(BaseModel):
    """RFC 7946 GeoJSON Feature representing a single topological anomaly in WGS 84 (EPSG:4326)."""

    type: Literal["Feature"] = Field(default="Feature", description="GeoJSON Feature type discriminator")
    id: Optional[str] = Field(default=None, description="Unique UUID of the topological error")
    geometry: Optional[Dict[str, Any]] = Field(default=None, description="RFC 7946 WGS 84 geometry object")
    properties: TopologyErrorProperties = Field(description="Topological conflict attributes and metrics")


class TopologyErrorFeatureCollection(BaseModel):
    """RFC 7946 GeoJSON FeatureCollection of topological errors for a cadastral layer."""

    type: Literal["FeatureCollection"] = Field(
        default="FeatureCollection",
        description="GeoJSON FeatureCollection type discriminator",
    )
    layer_id: str = Field(description="UUID of the inspected cadastral layer")
    total_count: int = Field(default=0, ge=0, description="Total number of topological errors in response")
    auto_resolvable_count: int = Field(default=0, ge=0, description="Count of AUTO_RESOLVABLE conflicts")
    critical_dispute_count: int = Field(default=0, ge=0, description="Count of CRITICAL_DISPUTE conflicts")
    resolved_count: int = Field(default=0, ge=0, description="Count of resolved conflicts")
    unresolved_count: int = Field(default=0, ge=0, description="Count of unresolved conflicts")
    counts_by_type: Dict[str, int] = Field(
        default_factory=dict,
        description="Breakdown of conflict counts keyed by TopologyErrorType",
    )
    bbox: Optional[List[float]] = Field(
        default=None,
        description="RFC 7946 WGS 84 bounding box [min_lon, min_lat, max_lon, max_lat]",
    )
    features: List[TopologyErrorFeature] = Field(
        default_factory=list,
        description="Ordered list of GeoJSON conflict features",
    )


def build_topology_error_feature_collection(
    layer_id: uuid.UUID | str,
    errors: Sequence[TopologicalError],
) -> TopologyErrorFeatureCollection:
    """Serialize a sequence of `TopologicalError` records into an RFC 7946 `TopologyErrorFeatureCollection`."""
    features: list[TopologyErrorFeature] = []
    min_lon: float | None = None
    min_lat: float | None = None
    max_lon: float | None = None
    max_lat: float | None = None

    for err in errors:
        raw_feature = err.to_geojson_feature()
        geom_dict = raw_feature.get("geometry")

        bounds: tuple[float, float, float, float] | None = None
        if err.error_geometry_wgs84 is not None:
            try:
                shp = to_shape(err.error_geometry_wgs84)
                if not shp.is_empty:
                    bounds = shp.bounds
            except Exception:
                bounds = None
        if bounds is None and geom_dict is not None:
            try:
                shp = shapely_shape(geom_dict)
                if not shp.is_empty:
                    bounds = shp.bounds
            except Exception:
                bounds = None

        if bounds is not None:
            gx0, gy0, gx1, gy1 = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
            min_lon = gx0 if min_lon is None else min(min_lon, gx0)
            min_lat = gy0 if min_lat is None else min(min_lat, gy0)
            max_lon = gx1 if max_lon is None else max(max_lon, gx1)
            max_lat = gy1 if max_lat is None else max(max_lat, gy1)

        features.append(
            TopologyErrorFeature(
                id=raw_feature.get("id"),
                geometry=geom_dict,
                properties=TopologyErrorProperties(**raw_feature["properties"]),
            )
        )

    bbox: list[float] | None = None
    if min_lon is not None and min_lat is not None and max_lon is not None and max_lat is not None:
        bbox = [min_lon, min_lat, max_lon, max_lat]

    counts_by_type = dict(Counter(e.error_type for e in errors))
    auto_count = sum(1 for e in errors if e.severity == TopologySeverity.AUTO_RESOLVABLE.value)
    critical_count = sum(1 for e in errors if e.severity == TopologySeverity.CRITICAL_DISPUTE.value)
    resolved_count = sum(1 for e in errors if e.is_resolved)
    unresolved_count = len(errors) - resolved_count

    return TopologyErrorFeatureCollection(
        layer_id=str(layer_id),
        total_count=len(errors),
        auto_resolvable_count=auto_count,
        critical_dispute_count=critical_count,
        resolved_count=resolved_count,
        unresolved_count=unresolved_count,
        counts_by_type=counts_by_type,
        bbox=bbox,
        features=features,
    )
