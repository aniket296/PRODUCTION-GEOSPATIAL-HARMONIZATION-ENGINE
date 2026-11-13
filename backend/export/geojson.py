"""RFC 7946 GeoJSON FeatureCollection exporter in canonical WGS 84 (Step 112/150).

Directive Sections 26-28:
- Exports harmonized cadastral parcels as a strict RFC 7946 GeoJSON `FeatureCollection` in WGS 84 (`EPSG:4326`),
  omitting the deprecated `"crs"` member.
- Enforces RFC 7946 Section 3.1.6 winding order: counter-clockwise (positive signed area) for exterior rings and
  clockwise (negative signed area) for interior hole rings via `enforce_rfc7946_winding_order`.
- Retains all cadastral metrics, dispute/resolution flags, and custom `attributes`, excludes merged audit slivers by
  default, and sorts features deterministically by `parcel.id`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.geometry.base import BaseGeometry

from backend.ingestion.geojson_reader import enforce_rfc7946_winding_order, round_coordinates
from backend.models.parcel import HarmonizedParcel

DEFAULT_COORDINATE_PRECISION: int = 7


def _is_merged_sliver(parcel: HarmonizedParcel) -> bool:
    attrs = parcel.attributes or {}
    return bool(attrs.get("merged_into_parcel_id"))


def _to_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    return MultiPolygon(polys)


def signed_ring_area(coords: Sequence[Sequence[float]]) -> float:
    """Compute signed 2D shoelace area of a coordinate ring (> 0 = CCW, < 0 = CW)."""
    if len(coords) < 3:
        return 0.0
    total = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = float(coords[i][0]), float(coords[i][1])
        x2, y2 = float(coords[i + 1][0]), float(coords[i + 1][1])
        total += x1 * y2 - x2 * y1
    return 0.5 * total


def export_parcels_to_geojson_dict(
    parcels: Sequence[HarmonizedParcel],
    *,
    include_merged_slivers: bool = False,
    coordinate_precision: int = DEFAULT_COORDINATE_PRECISION,
) -> dict[str, Any]:
    """Serialize `HarmonizedParcel` records into an RFC 7946 GeoJSON `FeatureCollection` dictionary."""
    if coordinate_precision < 1:
        raise ValueError("coordinate_precision must be >= 1")

    active = sorted(
        (p for p in parcels if include_merged_slivers or not _is_merged_sliver(p)),
        key=lambda p: str(p.id),
    )
    if not active:
        return {"type": "FeatureCollection", "features": []}

    features: list[dict[str, Any]] = []
    min_lon = float("inf")
    min_lat = float("inf")
    max_lon = float("-inf")
    max_lat = float("-inf")

    for parcel in active:
        mp = _to_multipolygon(to_shape(parcel.geometry_wgs84))
        rounded = round_coordinates(mp, precision=coordinate_precision)
        wound, _ = enforce_rfc7946_winding_order(rounded)
        b = wound.bounds
        min_lon = min(min_lon, float(b[0]))
        min_lat = min(min_lat, float(b[1]))
        max_lon = max(max_lon, float(b[2]))
        max_lat = max(max_lat, float(b[3]))

        features.append(
            {
                "type": "Feature",
                "id": str(parcel.id),
                "geometry": mapping(wound),
                "properties": {
                    "parcel_id": str(parcel.id),
                    "parcel_identifier": parcel.parcel_identifier,
                    "original_parcel_id": parcel.original_parcel_id,
                    "metric_area_sqm": parcel.metric_area_sqm,
                    "metric_perimeter_m": parcel.metric_perimeter_m,
                    "polsby_popper_compactness": parcel.polsby_popper_compactness,
                    "is_sliver": bool(parcel.is_sliver),
                    "is_disputed": bool(parcel.is_disputed),
                    "dispute_locked": bool(parcel.dispute_locked),
                    "resolution_status": parcel.resolution_status,
                    "attributes": parcel.attributes,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "bbox": [
            round(min_lon, coordinate_precision),
            round(min_lat, coordinate_precision),
            round(max_lon, coordinate_precision),
            round(max_lat, coordinate_precision),
        ],
        "features": features,
    }


def export_parcels_to_geojson_bytes(
    parcels: Sequence[HarmonizedParcel],
    *,
    include_merged_slivers: bool = False,
    coordinate_precision: int = DEFAULT_COORDINATE_PRECISION,
) -> bytes:
    """Serialize `HarmonizedParcel` records into deterministic UTF-8 RFC 7946 GeoJSON bytes."""
    fc = export_parcels_to_geojson_dict(
        parcels,
        include_merged_slivers=include_merged_slivers,
        coordinate_precision=coordinate_precision,
    )
    return json.dumps(fc, separators=(",", ":"), sort_keys=True).encode("utf-8")
