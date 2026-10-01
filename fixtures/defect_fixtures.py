"""Deterministic topological defect injection and synthetic GCP fixtures."""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Tuple
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import transform

from fixtures.cadastral_fixtures import (
    EVEREST_1830_CRS,
    UTM_ZONE_43N_CRS,
    WGS84_GEO_CRS,
    create_rectangular_polygon,
    generate_clean_cadastral_fixtures,
    transformer_everest_to_utm,
    transformer_everest_to_wgs84,
)

# Defect Classification Constants
DEFECT_MICRO_OVERLAP = "micro_overlap"
DEFECT_CRITICAL_OVERLAP = "critical_dispute_overlap"
DEFECT_BOUNDARY_GAP = "boundary_gap"
DEFECT_SLIVER_POLYGON = "sliver_polygon"
DEFECT_SELF_INTERSECTION = "self_intersection"
DEFECT_UNCLOSED_RING = "unclosed_ring"


def inject_micro_overlap(
    parcel_a: Dict[str, Any],
    parcel_b: Dict[str, Any],
    overlap_distance: float = 0.05,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Inject a deterministic micro-overlap (< tolerance, e.g. 0.05m) between two adjacent parcels.

    Expands parcel_a boundary eastwards into parcel_b by overlap_distance meters.
    Returns: (modified_parcel_a, parcel_b, defect_metadata)
    """
    poly_a: Polygon = parcel_a["geometry_everest"]
    minx, miny, maxx, maxy = poly_a.bounds

    # Shift the eastern edge eastwards by overlap_distance
    new_coords = [
        (minx, miny),
        (maxx + overlap_distance, miny),
        (maxx + overlap_distance, maxy),
        (minx, maxy),
        (minx, miny),
    ]
    mod_poly_a = Polygon(new_coords)
    mod_a = copy.deepcopy(parcel_a)
    mod_a["geometry_everest"] = mod_poly_a
    mod_a["geometry_wgs84"] = transform(transformer_everest_to_wgs84.transform, mod_poly_a)
    mod_a["geometry_utm"] = transform(transformer_everest_to_utm.transform, mod_poly_a)
    mod_a["area_sqm"] = float(mod_poly_a.area)
    mod_a["perimeter_m"] = float(mod_poly_a.length)
    mod_a["bounds"] = mod_poly_a.bounds

    intersection_geom = mod_poly_a.intersection(parcel_b["geometry_everest"])
    defect_info = {
        "defect_type": DEFECT_MICRO_OVERLAP,
        "parcel_ids": [parcel_a["parcel_identifier"], parcel_b["parcel_identifier"]],
        "overlap_distance_m": overlap_distance,
        "overlap_area_sqm": float(intersection_geom.area),
        "geometry": intersection_geom,
    }
    return mod_a, parcel_b, defect_info


def inject_critical_dispute_overlap(
    parcel_a: Dict[str, Any],
    parcel_b: Dict[str, Any],
    encroachment_distance: float = 2.5,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Inject a significant dispute overlap (> tolerance, e.g. 2.5m = 125 sqm) between adjacent parcels."""
    poly_a: Polygon = parcel_a["geometry_everest"]
    minx, miny, maxx, maxy = poly_a.bounds

    new_coords = [
        (minx, miny),
        (maxx + encroachment_distance, miny),
        (maxx + encroachment_distance, maxy),
        (minx, maxy),
        (minx, miny),
    ]
    mod_poly_a = Polygon(new_coords)
    mod_a = copy.deepcopy(parcel_a)
    mod_a["geometry_everest"] = mod_poly_a
    mod_a["geometry_wgs84"] = transform(transformer_everest_to_wgs84.transform, mod_poly_a)
    mod_a["geometry_utm"] = transform(transformer_everest_to_utm.transform, mod_poly_a)
    mod_a["area_sqm"] = float(mod_poly_a.area)
    mod_a["perimeter_m"] = float(mod_poly_a.length)
    mod_a["bounds"] = mod_poly_a.bounds

    intersection_geom = mod_poly_a.intersection(parcel_b["geometry_everest"])
    defect_info = {
        "defect_type": DEFECT_CRITICAL_OVERLAP,
        "parcel_ids": [parcel_a["parcel_identifier"], parcel_b["parcel_identifier"]],
        "overlap_distance_m": encroachment_distance,
        "overlap_area_sqm": float(intersection_geom.area),
        "geometry": intersection_geom,
    }
    return mod_a, parcel_b, defect_info


def inject_boundary_gap(
    parcel_a: Dict[str, Any],
    parcel_b: Dict[str, Any],
    gap_distance: float = 0.5,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Inject an unrecorded gap (0.5m) between two adjacent parcels by shrinking parcel_a."""
    poly_a: Polygon = parcel_a["geometry_everest"]
    minx, miny, maxx, maxy = poly_a.bounds

    # Retract the eastern edge westwards by gap_distance
    new_coords = [
        (minx, miny),
        (maxx - gap_distance, miny),
        (maxx - gap_distance, maxy),
        (minx, maxy),
        (minx, miny),
    ]
    mod_poly_a = Polygon(new_coords)
    mod_a = copy.deepcopy(parcel_a)
    mod_a["geometry_everest"] = mod_poly_a
    mod_a["geometry_wgs84"] = transform(transformer_everest_to_wgs84.transform, mod_poly_a)
    mod_a["geometry_utm"] = transform(transformer_everest_to_utm.transform, mod_poly_a)
    mod_a["area_sqm"] = float(mod_poly_a.area)
    mod_a["perimeter_m"] = float(mod_poly_a.length)
    mod_a["bounds"] = mod_poly_a.bounds

    gap_polygon = create_rectangular_polygon(maxx - gap_distance, miny, maxx, maxy)
    defect_info = {
        "defect_type": DEFECT_BOUNDARY_GAP,
        "parcel_ids": [parcel_a["parcel_identifier"], parcel_b["parcel_identifier"]],
        "gap_width_m": gap_distance,
        "gap_area_sqm": float(gap_polygon.area),
        "geometry": gap_polygon,
    }
    return mod_a, parcel_b, defect_info


def create_sliver_polygon(
    base_x: float,
    base_y: float,
    length: float = 50.0,
    width: float = 0.08,
) -> Dict[str, Any]:
    """Generate an extremely narrow sliver parcel polygon with very low Polsby-Popper compactness."""
    poly_everest = create_rectangular_polygon(base_x, base_y, base_x + width, base_y + length)
    poly_wgs84 = transform(transformer_everest_to_wgs84.transform, poly_everest)
    poly_utm = transform(transformer_everest_to_utm.transform, poly_everest)
    area = float(poly_everest.area)
    perimeter = float(poly_everest.length)
    compactness = (4.0 * math.pi * area) / (perimeter * perimeter)

    return {
        "parcel_identifier": "SLIVER-999",
        "khasra_number": "999-S",
        "defect_type": DEFECT_SLIVER_POLYGON,
        "geometry_everest": poly_everest,
        "geometry_wgs84": poly_wgs84,
        "geometry_utm": poly_utm,
        "area_sqm": area,
        "perimeter_m": perimeter,
        "compactness": compactness,
        "bounds": poly_everest.bounds,
    }


def create_self_intersecting_polygon(base_x: float, base_y: float) -> Polygon:
    """Create an invalid bow-tie (self-intersecting) polygon."""
    return Polygon(
        [
            (base_x, base_y),
            (base_x + 50.0, base_y + 50.0),
            (base_x + 50.0, base_y),
            (base_x, base_y + 50.0),
            (base_x, base_y),
        ]
    )


def generate_synthetic_gcp_fixtures(count: int = 6) -> List[Dict[str, Any]]:
    """Generate deterministic Ground Control Points (GCPs) for transformation calibration.

    Includes exact coordinate pairs across Everest 1830 and UTM Zone 43N.
    """
    base_x = 3600000.0
    base_y = 500000.0

    raw_gcps = [
        ("GCP-01", base_x + 0.0, base_y + 0.0),
        ("GCP-02", base_x + 220.0, base_y + 0.0),
        ("GCP-03", base_x + 220.0, base_y + 100.0),
        ("GCP-04", base_x + 0.0, base_y + 100.0),
        ("GCP-05", base_x + 110.0, base_y + 50.0),
        ("GCP-06", base_x + 90.0, base_y + 25.0),
    ]

    gcps = []
    for gcp_id, ev_x, ev_y in raw_gcps[:count]:
        pt_ev = Point(ev_x, ev_y)
        pt_wgs = transform(transformer_everest_to_wgs84.transform, pt_ev)
        pt_utm = transform(transformer_everest_to_utm.transform, pt_ev)

        gcps.append(
            {
                "point_id": gcp_id,
                "source_x": float(ev_x),
                "source_y": float(ev_y),
                "source_crs": EVEREST_1830_CRS,
                "target_x": float(pt_utm.x),
                "target_y": float(pt_utm.y),
                "target_crs": UTM_ZONE_43N_CRS,
                "wgs84_lon": float(pt_wgs.x),
                "wgs84_lat": float(pt_wgs.y),
            }
        )

    return gcps
