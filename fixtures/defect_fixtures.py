"""Deterministic topological defect injection and synthetic DXF/GCP fixtures."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from shapely.geometry import Point, Polygon
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
        "owner_name": "Disputed Sliver Strip",
        "land_use": "Unclassified",
        "source_crs": EVEREST_1830_CRS,
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


def generate_defective_cadastral_dataset() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Generate a realistic cadastral dataset with deterministic topological defects injected.

    Injected defects:
    1. Micro-overlap between KHASRA-101 and KHASRA-102 (0.05m encroachment).
    2. Critical dispute overlap between KHASRA-103 and KHASRA-104 (2.0m encroachment = 100 sqm).
    3. Boundary gap between KHASRA-107 and KHASRA-108 (0.5m gap = 25 sqm).
    4. Sliver parcel polygon inserted along eastern boundary.

    Returns:
        (parcels, defect_manifest)
    """
    clean_parcels = generate_clean_cadastral_fixtures()
    parcels_map = {p["parcel_identifier"]: copy.deepcopy(p) for p in clean_parcels}
    defects_manifest: List[Dict[str, Any]] = []

    # 1. Micro-overlap: KHASRA-101 into KHASRA-102
    mod_101, _, defect_micro = inject_micro_overlap(
        parcels_map["KHASRA-101"], parcels_map["KHASRA-102"], overlap_distance=0.05
    )
    parcels_map["KHASRA-101"] = mod_101
    defects_manifest.append(defect_micro)

    # 2. Critical dispute overlap: KHASRA-103 into KHASRA-104 (2.0m)
    mod_103, _, defect_critical = inject_critical_dispute_overlap(
        parcels_map["KHASRA-103"], parcels_map["KHASRA-104"], encroachment_distance=2.0
    )
    parcels_map["KHASRA-103"] = mod_103
    defects_manifest.append(defect_critical)

    # 3. Boundary gap: KHASRA-107 and KHASRA-108 (0.5m)
    mod_107, _, defect_gap = inject_boundary_gap(
        parcels_map["KHASRA-107"], parcels_map["KHASRA-108"], gap_distance=0.5
    )
    parcels_map["KHASRA-107"] = mod_107
    defects_manifest.append(defect_gap)

    # 4. Sliver parcel (SLIVER-999) along boundary
    base_x = 3600220.0
    base_y = 500000.0
    sliver = create_sliver_polygon(base_x=base_x, base_y=base_y, length=50.0, width=0.08)
    defects_manifest.append(
        {
            "defect_type": DEFECT_SLIVER_POLYGON,
            "parcel_ids": [sliver["parcel_identifier"]],
            "area_sqm": sliver["area_sqm"],
            "compactness": sliver["compactness"],
            "geometry": sliver["geometry_everest"],
        }
    )

    result_parcels = list(parcels_map.values()) + [sliver]
    return result_parcels, defects_manifest


def generate_synthetic_gcp_fixtures(
    count: int = 6,
    residual_noise_m: float = 0.0,
) -> List[Dict[str, Any]]:
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
    for idx, (gcp_id, ev_x, ev_y) in enumerate(raw_gcps[:count]):
        pt_ev = Point(ev_x, ev_y)
        pt_wgs = transform(transformer_everest_to_wgs84.transform, pt_ev)
        pt_utm = transform(transformer_everest_to_utm.transform, pt_ev)

        # Apply deterministic offset if residual noise requested
        target_x = float(pt_utm.x)
        target_y = float(pt_utm.y)
        if residual_noise_m > 0:
            target_x += residual_noise_m * (1 if idx % 2 == 0 else -1)
            target_y += residual_noise_m * (1 if idx % 3 == 0 else -1)

        gcps.append(
            {
                "point_id": gcp_id,
                "source_x": float(ev_x),
                "source_y": float(ev_y),
                "source_crs": EVEREST_1830_CRS,
                "target_x": target_x,
                "target_y": target_y,
                "target_crs": UTM_ZONE_43N_CRS,
                "wgs84_lon": float(pt_wgs.x),
                "wgs84_lat": float(pt_wgs.y),
            }
        )

    return gcps


def generate_synthetic_dxf_cadastre(
    parcels: Optional[List[Dict[str, Any]]] = None,
    geometry_key: str = "geometry_everest",
) -> str:
    """Generate a standard compliant ASCII DXF file representing cadastral parcel boundaries.

    Uses standard DXF group codes:
    - 0: Entity type (SECTION, TABLE, LAYER, LWPOLYLINE, POINT, ENDSEC, EOF)
    - 2: Name / Layer / Section
    - 8: Layer name
    - 70: Polyline flags (1 = closed)
    - 90: Number of vertices
    - 10: X coordinate
    - 20: Y coordinate
    """
    if parcels is None:
        parcels = generate_clean_cadastral_fixtures()

    lines: List[str] = [
        "0", "SECTION",
        "2", "HEADER",
        "9", "$ACADVER",
        "1", "AC1015",  # AutoCAD 2000 DXF format
        "0", "ENDSEC",
        "0", "SECTION",
        "2", "TABLES",
        "0", "TABLE",
        "2", "LAYER",
        "70", "2",
        "0", "LAYER",
        "2", "PARCELS",
        "70", "0",
        "62", "7",
        "6", "CONTINUOUS",
        "0", "LAYER",
        "2", "SURVEY_GCP",
        "70", "0",
        "62", "1",
        "6", "CONTINUOUS",
        "0", "ENDTAB",
        "0", "ENDSEC",
        "0", "SECTION",
        "2", "ENTITIES",
    ]

    for p in parcels:
        poly: Polygon = p[geometry_key]
        coords = list(poly.exterior.coords)[:-1]  # Exclude closing repeated vertex for LWPOLYLINE
        vertex_count = len(coords)

        lines.extend([
            "0", "LWPOLYLINE",
            "5", hex(hash(p["parcel_identifier"]) & 0xFFFF)[2:].upper().zfill(4),
            "8", "PARCELS",
            "100", "AcDbEntity",
            "100", "AcDbPolyline",
            "90", str(vertex_count),
            "70", "1",  # 1 = closed polygon
            "43", "0.0",
        ])

        for vx, vy in coords:
            lines.extend([
                "10", f"{vx:.4f}",
                "20", f"{vy:.4f}",
            ])

    # Append Survey GCP points
    gcps = generate_synthetic_gcp_fixtures(count=4)
    for gcp in gcps:
        lines.extend([
            "0", "POINT",
            "8", "SURVEY_GCP",
            "10", f"{gcp['source_x']:.4f}",
            "20", f"{gcp['source_y']:.4f}",
            "30", "0.0",
        ])

    lines.extend([
        "0", "ENDSEC",
        "0", "EOF",
        "",
    ])

    return "\n".join(lines)


def save_synthetic_dxf(
    output_path: Union[str, Path],
    parcels: Optional[List[Dict[str, Any]]] = None,
    geometry_key: str = "geometry_everest",
) -> Path:
    """Write synthetic cadastral DXF file to disk."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dxf_content = generate_synthetic_dxf_cadastre(parcels=parcels, geometry_key=geometry_key)
    with open(path, "w", encoding="utf-8") as f:
        f.write(dxf_content)
    return path
