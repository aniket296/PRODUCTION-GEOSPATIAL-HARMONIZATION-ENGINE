"""Tests for deterministic topological defect injection and synthetic GCP fixtures."""

import math
import pytest
from shapely.geometry import Point, Polygon

from fixtures.cadastral_fixtures import generate_clean_cadastral_fixtures
from fixtures.defect_fixtures import (
    DEFECT_BOUNDARY_GAP,
    DEFECT_CRITICAL_OVERLAP,
    DEFECT_MICRO_OVERLAP,
    DEFECT_SLIVER_POLYGON,
    create_self_intersecting_polygon,
    create_sliver_polygon,
    generate_synthetic_gcp_fixtures,
    inject_boundary_gap,
    inject_critical_dispute_overlap,
    inject_micro_overlap,
)


def test_micro_overlap_injection():
    clean_parcels = generate_clean_cadastral_fixtures()
    p101 = clean_parcels[0]
    p102 = clean_parcels[1]

    mod_a, p_b, defect = inject_micro_overlap(p101, p102, overlap_distance=0.05)

    assert defect["defect_type"] == DEFECT_MICRO_OVERLAP
    assert defect["parcel_ids"] == ["KHASRA-101", "KHASRA-102"]
    assert defect["overlap_distance_m"] == 0.05

    # 0.05m overlap * 50m height = 2.5 sqm
    assert pytest.approx(defect["overlap_area_sqm"], abs=1e-3) == 2.5
    assert defect["geometry"].is_valid is True
    assert isinstance(defect["geometry"], Polygon)

    # Intersection between modified a and b matches defect area
    actual_intersection = mod_a["geometry_everest"].intersection(p_b["geometry_everest"])
    assert pytest.approx(actual_intersection.area, abs=1e-3) == 2.5


def test_critical_dispute_overlap_injection():
    clean_parcels = generate_clean_cadastral_fixtures()
    p101 = clean_parcels[0]
    p102 = clean_parcels[1]

    mod_a, p_b, defect = inject_critical_dispute_overlap(p101, p102, encroachment_distance=2.5)

    assert defect["defect_type"] == DEFECT_CRITICAL_OVERLAP
    assert defect["parcel_ids"] == ["KHASRA-101", "KHASRA-102"]
    assert defect["overlap_distance_m"] == 2.5

    # 2.5m overlap * 50m height = 125.0 sqm
    assert pytest.approx(defect["overlap_area_sqm"], abs=1e-3) == 125.0
    assert defect["geometry"].is_valid is True
    assert defect["overlap_area_sqm"] > 5.0  # Well above automated resolution threshold


def test_boundary_gap_injection():
    clean_parcels = generate_clean_cadastral_fixtures()
    p101 = clean_parcels[0]
    p102 = clean_parcels[1]

    mod_a, p_b, defect = inject_boundary_gap(p101, p102, gap_distance=0.5)

    assert defect["defect_type"] == DEFECT_BOUNDARY_GAP
    assert defect["gap_width_m"] == 0.5

    # 0.5m gap * 50m height = 25.0 sqm
    assert pytest.approx(defect["gap_area_sqm"], abs=1e-3) == 25.0
    assert defect["geometry"].is_valid is True

    # Modified parcel a and b no longer intersect or touch along that boundary
    assert mod_a["geometry_everest"].intersects(p_b["geometry_everest"]) is False
    assert mod_a["geometry_everest"].touches(p_b["geometry_everest"]) is False


def test_sliver_polygon_creation():
    sliver = create_sliver_polygon(base_x=3600000.0, base_y=500000.0, length=50.0, width=0.08)

    assert sliver["defect_type"] == DEFECT_SLIVER_POLYGON
    assert sliver["parcel_identifier"] == "SLIVER-999"

    # Area = 50 * 0.08 = 4.0 sqm
    assert pytest.approx(sliver["area_sqm"], abs=1e-3) == 4.0

    # Polsby-Popper compactness must be very low for a 50m x 0.08m sliver
    # Perimeter ~ 100.16m, Compactness = (4 * pi * 4) / (100.16^2) ~ 0.005
    assert sliver["compactness"] < 0.05
    assert sliver["geometry_everest"].is_valid is True


def test_self_intersecting_polygon_creation():
    from shapely.validation import explain_validity

    bow_tie = create_self_intersecting_polygon(base_x=3600000.0, base_y=500000.0)

    # In shapely, a self-intersecting polygon has is_valid == False
    assert bow_tie.is_valid is False
    assert "Self-intersection" in explain_validity(bow_tie)


def test_synthetic_gcp_fixtures():
    gcps = generate_synthetic_gcp_fixtures(count=6)

    assert len(gcps) == 6
    gcp_ids = [g["point_id"] for g in gcps]
    assert gcp_ids == ["GCP-01", "GCP-02", "GCP-03", "GCP-04", "GCP-05", "GCP-06"]

    for gcp in gcps:
        assert gcp["source_crs"] == "EPSG:24378"
        assert gcp["target_crs"] == "EPSG:32643"
        assert 3600000.0 <= gcp["source_x"] <= 3600220.0
        assert 500000.0 <= gcp["source_y"] <= 500100.0
        assert 68.0 <= gcp["wgs84_lon"] <= 90.0
        assert 10.0 <= gcp["wgs84_lat"] <= 35.0
        assert gcp["target_x"] > 0
        assert gcp["target_y"] > 0
