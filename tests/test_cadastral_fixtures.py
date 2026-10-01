"""Tests for deterministic Everest 1830 cadastral test fixtures, CRS specifications, bounds, and topology."""

import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from fixtures.cadastral_fixtures import (
    EVEREST_1830_CRS,
    UTM_ZONE_43N_CRS,
    WGS84_GEO_CRS,
    generate_clean_cadastral_fixtures,
    get_fixture_bounds,
)


def test_crs_identifiers():
    assert EVEREST_1830_CRS == "EPSG:24378"
    assert UTM_ZONE_43N_CRS == "EPSG:32643"
    assert WGS84_GEO_CRS == "EPSG:4326"


def test_clean_fixture_cardinality_and_khasra_identifiers():
    parcels = generate_clean_cadastral_fixtures()
    assert len(parcels) == 10

    identifiers = [p["parcel_identifier"] for p in parcels]
    expected_ids = [f"KHASRA-{101 + i}" for i in range(10)]
    assert identifiers == expected_ids
    assert len(set(identifiers)) == 10

    khasra_nums = [p["khasra_number"] for p in parcels]
    assert khasra_nums == [str(101 + i) for i in range(10)]

    land_uses = {p["land_use"] for p in parcels}
    assert "Agricultural" in land_uses
    assert "Residential" in land_uses
    assert "Commercial" in land_uses
    assert "Government" in land_uses


def test_parcel_geometry_validity_and_bounds():
    parcels = generate_clean_cadastral_fixtures()

    for p in parcels:
        geom = p["geometry_everest"]
        assert isinstance(geom, Polygon)
        assert geom.is_valid is True
        assert not geom.is_empty
        assert geom.area > 0

    minx, miny, maxx, maxy = get_fixture_bounds(parcels)
    assert minx == 3600000.0
    assert miny == 500000.0
    assert maxx == 3600220.0
    assert maxy == 500100.0


def test_parcel_metric_areas_and_compactness():
    parcels = generate_clean_cadastral_fixtures()

    total_sum_area = sum(p["area_sqm"] for p in parcels)
    # 220m width * 100m height = 22,000 sqm
    assert total_sum_area == 22000.0

    for p in parcels:
        area = p["area_sqm"]
        perimeter = p["perimeter_m"]
        assert 1500.0 <= area <= 3000.0

        # Calculate Polsby-Popper compactness: (4 * pi * A) / P^2
        import math
        polsby_popper = (4.0 * math.pi * area) / (perimeter * perimeter)
        # Rectangles with aspect ratios 4:5 and 1:1 have Polsby-Popper > 0.75
        assert polsby_popper > 0.70, f"Compactness {polsby_popper} unexpectedly low for standard parcel"


def test_clean_fixture_zero_overlaps_and_gaps():
    parcels = generate_clean_cadastral_fixtures()
    geoms = [p["geometry_everest"] for p in parcels]

    # Check pair-wise topological orthogonality: overlap area must be exactly 0
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            intersection = geoms[i].intersection(geoms[j])
            assert (
                intersection.area == 0.0
            ), f"Parcels {parcels[i]['parcel_identifier']} and {parcels[j]['parcel_identifier']} have non-zero overlap"

    # Check contiguous block union has zero interior gaps or holes
    union_poly = unary_union(geoms)
    assert union_poly.is_valid is True
    assert union_poly.area == 22000.0
    assert len(union_poly.interiors) == 0  # No interior holes/gaps


def test_reprojected_wgs84_and_utm_geometries_valid():
    parcels = generate_clean_cadastral_fixtures()

    for p in parcels:
        geom_wgs84 = p["geometry_wgs84"]
        assert geom_wgs84.is_valid is True
        assert geom_wgs84.area > 0

        # Verify Indian subcontinent bounding coordinates
        wgs_minx, wgs_miny, wgs_maxx, wgs_maxy = geom_wgs84.bounds
        assert 68.0 <= wgs_minx <= 90.0, f"Longitude {wgs_minx} outside India geodetic extent"
        assert 10.0 <= wgs_miny <= 35.0, f"Latitude {wgs_miny} outside India geodetic extent"

        geom_utm = p["geometry_utm"]
        assert geom_utm.is_valid is True
        assert geom_utm.area > 0
