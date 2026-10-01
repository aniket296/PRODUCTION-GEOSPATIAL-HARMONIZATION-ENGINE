"""Deterministic cadastral test fixtures for Everest 1830 / Kalianpur 1975 grid."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
import pyproj
from shapely.geometry import Polygon, mapping
from shapely.ops import transform

# EPSG Coordinate Reference System definitions
EVEREST_1830_CRS = "EPSG:24378"  # Kalianpur 1975 / India Zone IIa (Metric Grid)
UTM_ZONE_43N_CRS = "EPSG:32643"  # WGS 84 / UTM Zone 43N
WGS84_GEO_CRS = "EPSG:4326"      # Canonical Geographic WGS 84

# Coordinate transformers
transformer_everest_to_wgs84 = pyproj.Transformer.from_crs(
    EVEREST_1830_CRS, WGS84_GEO_CRS, always_xy=True
)
transformer_everest_to_utm = pyproj.Transformer.from_crs(
    EVEREST_1830_CRS, UTM_ZONE_43N_CRS, always_xy=True
)


def create_rectangular_polygon(x1: float, y1: float, x2: float, y2: float) -> Polygon:
    """Create a closed rectangular shapely Polygon."""
    return Polygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)])


def generate_clean_cadastral_fixtures() -> List[Dict[str, Any]]:
    """Generate 10 deterministic contiguous cadastral parcels in Kalianpur 1975 (EPSG:24378).

    The parcels form a perfectly contiguous 2x5 cadastral block representing
    an authentic revenue village (Khasra 101 to 110) with exact coincident boundaries
    and zero topological gaps or overlaps.
    """
    # Origin coordinates in Kalianpur 1975 Zone IIa (approx. Central/Northern India: 76.75°E, 28.44°N)
    base_x = 3600000.0
    base_y = 500000.0

    # Parcel dimensional layout (x_start, y_start, x_end, y_end, khasra, owner, land_use)
    parcel_specs = [
        # Row 1 (y: 2000000 -> 2000050)
        (base_x + 0.0, base_y + 0.0, base_x + 40.0, base_y + 50.0, "101", "Rajesh Sharma", "Agricultural"),
        (base_x + 40.0, base_y + 0.0, base_x + 90.0, base_y + 50.0, "102", "Priya Verma", "Agricultural"),
        (base_x + 90.0, base_y + 0.0, base_x + 130.0, base_y + 50.0, "103", "Suresh Patel", "Commercial"),
        (base_x + 130.0, base_y + 0.0, base_x + 180.0, base_y + 50.0, "104", "Anita Singh", "Residential"),
        (base_x + 180.0, base_y + 0.0, base_x + 220.0, base_y + 50.0, "105", "Gram Panchayat", "Government"),
        # Row 2 (y: 2000050 -> 2000100)
        (base_x + 0.0, base_y + 50.0, base_x + 40.0, base_y + 100.0, "106", "Vikram Rathore", "Agricultural"),
        (base_x + 40.0, base_y + 50.0, base_x + 90.0, base_y + 100.0, "107", "Sunita Yadav", "Agricultural"),
        (base_x + 90.0, base_y + 50.0, base_x + 130.0, base_y + 100.0, "108", "Manoj Joshi", "Residential"),
        (base_x + 130.0, base_y + 50.0, base_x + 180.0, base_y + 100.0, "109", "Deepak Gupta", "Residential"),
        (base_x + 180.0, base_y + 50.0, base_x + 220.0, base_y + 100.0, "110", "Kavita Tiwari", "Commercial"),
    ]

    fixtures: List[Dict[str, Any]] = []
    for x1, y1, x2, y2, khasra, owner, land_use in parcel_specs:
        poly_everest = create_rectangular_polygon(x1, y1, x2, y2)
        poly_wgs84 = transform(transformer_everest_to_wgs84.transform, poly_everest)
        poly_utm = transform(transformer_everest_to_utm.transform, poly_everest)

        fixtures.append(
            {
                "parcel_identifier": f"KHASRA-{khasra}",
                "khasra_number": khasra,
                "owner_name": owner,
                "land_use": land_use,
                "source_crs": EVEREST_1830_CRS,
                "geometry_everest": poly_everest,
                "geometry_wgs84": poly_wgs84,
                "geometry_utm": poly_utm,
                "area_sqm": float(poly_everest.area),
                "perimeter_m": float(poly_everest.length),
                "bounds": poly_everest.bounds,
            }
        )

    return fixtures


def get_fixture_bounds(parcels: List[Dict[str, Any]]) -> Tuple[float, float, float, float]:
    """Calculate combined spatial bounding box (minx, miny, maxx, maxy) of parcel collection."""
    minx = min(p["bounds"][0] for p in parcels)
    miny = min(p["bounds"][1] for p in parcels)
    maxx = max(p["bounds"][2] for p in parcels)
    maxy = max(p["bounds"][3] for p in parcels)
    return minx, miny, maxx, maxy
