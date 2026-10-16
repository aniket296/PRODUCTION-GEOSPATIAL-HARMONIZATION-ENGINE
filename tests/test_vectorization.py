"""Comprehensive test suite for automated polygon vectorization and Douglas-Peucker simplification from raster masks.

Validates:
1. Connected-component region extraction from binary segmentation / boundary edge masks.
2. Noise filtering (removing sub-threshold morphological speckles below min_area_sqm).
3. Pixel-to-world affine georeferencing transformation (origin_x, origin_y, gsd_m).
4. Staircase raster contour tracing into closed, topologically valid Shapely Polygons.
5. Douglas-Peucker simplification to eliminate pixel stair-stepping while preserving area and topology.
6. End-to-end PolygonVectorizationEngine pipeline with VectorizationAudit metrics.
"""

from __future__ import annotations

from typing import List
import numpy as np
import pytest
from shapely.geometry import Polygon

from backend.ai.vectorization import (
    AffineGeoTransform,
    BoundarySimplifier,
    ConnectedComponentExtractor,
    PolygonVectorizationEngine,
    VectorizationAudit,
    VectorizationConfig,
    VectorizedParcel,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def single_parcel_binary_mask() -> np.ndarray:
    """64x64 binary mask containing a single 20x30 rectangular parcel region (rows 10..30, cols 15..45)."""
    mask = np.zeros((64, 64), dtype=bool)
    mask[10:30, 15:45] = True
    return mask


@pytest.fixture
def multi_parcel_with_noise_mask() -> np.ndarray:
    """64x64 binary mask with two distinct parcels separated by a bund and 2 tiny noise speckles."""
    mask = np.zeros((64, 64), dtype=bool)
    # Parcel 1: 20x20 (400 px)
    mask[5:25, 5:25] = True
    # Parcel 2: 20x25 (500 px)
    mask[35:55, 30:55] = True
    # Noise speckle 1: 2x2 (4 px)
    mask[2:4, 50:52] = True
    # Noise speckle 2: 1x3 (3 px)
    mask[58:59, 10:13] = True
    return mask


@pytest.fixture
def staircase_diagonal_parcel_mask() -> np.ndarray:
    """80x80 binary mask with a rotated/diagonal polygon producing staircase pixel edges."""
    mask = np.zeros((80, 80), dtype=bool)
    # Diamond / rotated square centered at (40, 40) with L1 radius 25
    for r in range(80):
        for c in range(80):
            if abs(r - 40) + abs(c - 40) <= 24:
                mask[r, c] = True
    return mask


# =====================================================================
# Unit Tests: ConnectedComponentExtractor
# =====================================================================

class TestConnectedComponentExtractor:
    """Test connected component labeling and speckle noise filtering."""

    def test_extract_single_component(self, single_parcel_binary_mask: np.ndarray):
        extractor = ConnectedComponentExtractor(min_pixels=10)
        components = extractor.extract_components(single_parcel_binary_mask)

        assert len(components) == 1
        assert components[0].pixel_count == 20 * 30
        assert components[0].bbox_pixels == (10, 15, 30, 45)

    def test_filter_small_noise_speckles(self, multi_parcel_with_noise_mask: np.ndarray):
        extractor = ConnectedComponentExtractor(min_pixels=25)
        components = extractor.extract_components(multi_parcel_with_noise_mask)

        # Only the two main parcels (400 px and 500 px) should survive
        assert len(components) == 2
        counts = sorted(c.pixel_count for c in components)
        assert counts == [400, 500]


# =====================================================================
# Unit Tests: AffineGeoTransform
# =====================================================================

class TestAffineGeoTransform:
    """Test pixel (col, row) to projected geographic (x, y) coordinate mapping."""

    def test_pixel_to_world_mapping(self):
        # UTM origin (500000.0 E, 2800000.0 N), 2.5m GSD, north-up (row increases southward)
        transform = AffineGeoTransform(
            origin_x=500000.0,
            origin_y=2800000.0,
            gsd_x=2.5,
            gsd_y=-2.5,
        )

        x0, y0 = transform.pixel_to_world(col=0.0, row=0.0)
        assert x0 == 500000.0
        assert y0 == 2800000.0

        x1, y1 = transform.pixel_to_world(col=20.0, row=10.0)
        assert x1 == 500050.0
        assert y1 == 2799975.0


# =====================================================================
# Unit Tests: BoundarySimplifier
# =====================================================================

class TestBoundarySimplifier:
    """Test Douglas-Peucker simplification and staircase vertex reduction."""

    def test_simplify_staircase_polygon_reduces_vertices(self, staircase_diagonal_parcel_mask: np.ndarray):
        engine = PolygonVectorizationEngine(
            config=VectorizationConfig(
                gsd_m=1.0,
                simplify_tolerance_m=0.0,  # Raw unsimplified contour first
                min_area_sqm=20.0,
            )
        )
        raw_parcels, _ = engine.vectorize_foreground_mask(staircase_diagonal_parcel_mask)
        assert len(raw_parcels) == 1
        raw_poly = raw_parcels[0].polygon
        raw_vertex_count = len(raw_poly.exterior.coords)

        simplifier = BoundarySimplifier(tolerance_m=1.5, preserve_topology=True)
        simplified_poly = simplifier.simplify(raw_poly)
        simplified_vertex_count = len(simplified_poly.exterior.coords)

        assert simplified_poly.is_valid
        # Staircase diamond has dozens of step vertices; simplified diamond approaches 4-8 vertices
        assert simplified_vertex_count < raw_vertex_count * 0.4
        # Area should be preserved within 5%
        assert pytest.approx(raw_poly.area, rel=0.05) == simplified_poly.area

    def test_simplify_preserves_valid_topology(self):
        # L-shaped polygon
        poly = Polygon([(0, 0), (20, 0), (20, 10), (10, 10), (10, 20), (0, 20), (0, 0)])
        simplifier = BoundarySimplifier(tolerance_m=0.5, preserve_topology=True)
        simplified = simplifier.simplify(poly)

        assert simplified.is_valid
        assert not simplified.is_empty
        assert pytest.approx(poly.area, rel=0.01) == simplified.area


# =====================================================================
# Unit Tests: PolygonVectorizationEngine
# =====================================================================

class TestPolygonVectorizationEngine:
    """Test end-to-end raster mask to simplified vector polygon pipeline."""

    def test_vectorize_single_rectangular_parcel_with_georeferencing(
        self, single_parcel_binary_mask: np.ndarray
    ):
        # 20 rows x 30 cols at 2.5m GSD => 50m x 75m = 3750 sqm
        config = VectorizationConfig(
            origin_x=400000.0,
            origin_y=2900000.0,
            gsd_m=2.5,
            simplify_tolerance_m=1.0,
            min_area_sqm=50.0,
        )
        engine = PolygonVectorizationEngine(config=config)
        parcels, audit = engine.vectorize_foreground_mask(single_parcel_binary_mask)

        assert len(parcels) == 1
        p = parcels[0]
        assert isinstance(p, VectorizedParcel)
        assert p.polygon.is_valid
        assert pytest.approx(3750.0, rel=0.02) == p.area_sqm
        assert audit.total_parcels_extracted == 1
        assert audit.noise_components_filtered == 0
        assert audit.all_geometries_valid is True

    def test_vectorize_multi_parcel_filters_noise_and_audits(
        self, multi_parcel_with_noise_mask: np.ndarray
    ):
        # GSD = 2.0m => 1 pixel = 4 sqm
        # Noise speckles are 3 px (12 sqm) and 4 px (16 sqm)
        # Main parcels are 400 px (1600 sqm) and 500 px (2000 sqm)
        config = VectorizationConfig(
            gsd_m=2.0,
            simplify_tolerance_m=1.0,
            min_area_sqm=100.0,
        )
        engine = PolygonVectorizationEngine(config=config)
        parcels, audit = engine.vectorize_foreground_mask(multi_parcel_with_noise_mask)

        assert len(parcels) == 2
        assert audit.total_parcels_extracted == 2
        assert audit.noise_components_filtered == 2
        assert audit.all_geometries_valid is True

        areas = sorted(p.area_sqm for p in parcels)
        assert pytest.approx(1600.0, rel=0.02) == areas[0]
        assert pytest.approx(2000.0, rel=0.02) == areas[1]

        # Check non-overlapping between extracted parcels
        assert parcels[0].polygon.intersection(parcels[1].polygon).area == 0.0

    def test_vectorize_from_boundary_edge_mask(self):
        """Verify extracting enclosed parcel interior polygons from a binary boundary edge ring mask."""
        # 50x50 raster with a closed boundary ring around a parcel at [10:40, 10:40]
        edge_mask = np.zeros((50, 50), dtype=bool)
        edge_mask[10:41, 10:12] = True  # Left bund wall
        edge_mask[10:41, 39:41] = True  # Right bund wall
        edge_mask[10:12, 10:41] = True  # Top bund wall
        edge_mask[39:41, 10:41] = True  # Bottom bund wall

        config = VectorizationConfig(
            gsd_m=1.0,
            simplify_tolerance_m=0.8,
            min_area_sqm=100.0,
            max_area_sqm=1500.0,  # Exclude outer background border region
        )
        engine = PolygonVectorizationEngine(config=config)
        parcels, audit = engine.vectorize_edge_mask(edge_mask)

        assert len(parcels) == 1
        # Interior region is [12:39, 12:39] -> 27x27 = 729 sqm
        assert pytest.approx(729.0, rel=0.05) == parcels[0].area_sqm
        assert parcels[0].polygon.is_valid
        assert audit.all_geometries_valid is True
