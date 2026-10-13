"""Comprehensive test suite for vector field deformation and elastic cadastre boundary snapping.

Validates:
1. Spatial displacement vector field construction (sparse GCP offsets, dense grid interpolation, RBF / Gaussian kernel).
2. Elastic/thin-plate spline deformation field interpolation from boundary discrepancies.
3. Cadastre polygon warping / deformation along estimated displacement vectors (dx, dy).
4. Maximum allowable elasticity constraint (clamping max distortion per vertex to prevent unrealistic boundary stretching).
5. Topology preservation during deformation (validity checks, preventing self-intersections, no boundary inversions).
6. Shared boundary snapping (adjacent parcels deformed concurrently preserving shared boundary edge coincidence).
7. Comprehensive deformation audit metrics (RMS displacement, max displacement, area distortion ratio, topology check).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple
import pytest
from shapely.geometry import LineString, MultiPolygon, Polygon, box

from backend.ai.deformation import (
    DeformationAudit,
    DeformationConfig,
    DisplacementVector,
    ElasticDeformationEngine,
    VectorFieldInterpolator,
    apply_vector_field_to_polygon,
    build_displacement_field_from_pairs,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def sample_cadastre_parcel() -> Polygon:
    """100m x 100m regular square cadastral parcel."""
    return Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)])


@pytest.fixture
def adjacent_parcel_pair() -> Tuple[Polygon, Polygon]:
    """Two adjacent parcels sharing the boundary along x=100 from y=0 to y=100."""
    p1 = Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)])
    p2 = Polygon([(100.0, 0.0), (200.0, 0.0), (200.0, 100.0), (100.0, 100.0), (100.0, 0.0)])
    return p1, p2


# =====================================================================
# 1. Displacement Vector & Vector Field Interpolator Tests
# =====================================================================

class TestVectorFieldInterpolator:
    """Tests for continuous displacement vector fields from sparse control points."""

    def test_single_vector_uniform_translation(self, sample_cadastre_parcel: Polygon):
        # A single displacement vector at (50, 50) of dx=10, dy=5
        vectors = [DisplacementVector(x=50.0, y=50.0, dx=10.0, dy=5.0, weight=1.0)]
        interpolator = VectorFieldInterpolator(vectors, kernel="idw", power=2.0)

        # Sampling near the point should yield roughly dx=10, dy=5
        sampled_dx, sampled_dy = interpolator.interpolate(50.0, 50.0)
        assert pytest.approx(sampled_dx, abs=1e-4) == 10.0
        assert pytest.approx(sampled_dy, abs=1e-4) == 5.0

    def test_multi_point_gaussian_rbf_interpolation(self):
        vectors = [
            DisplacementVector(x=0.0, y=0.0, dx=4.0, dy=0.0),
            DisplacementVector(x=100.0, y=0.0, dx=4.0, dy=0.0),
            DisplacementVector(x=100.0, y=100.0, dx=0.0, dy=4.0),
            DisplacementVector(x=0.0, y=100.0, dx=0.0, dy=4.0),
        ]
        interpolator = VectorFieldInterpolator(vectors, kernel="rbf", epsilon=50.0)

        # Midpoint on bottom edge (50, 0) should have dx > 0 and dy near 0
        dx_bottom, dy_bottom = interpolator.interpolate(50.0, 0.0)
        assert dx_bottom > 2.0
        assert abs(dy_bottom) < 1.0

        # Midpoint on top edge (50, 100) should have dy > 0 and dx near 0
        dx_top, dy_top = interpolator.interpolate(50.0, 100.0)
        assert dy_top > 2.0
        assert abs(dx_top) < 1.0

    def test_empty_vectors_returns_zero_displacement(self):
        interpolator = VectorFieldInterpolator([], kernel="idw")
        dx, dy = interpolator.interpolate(25.0, 30.0)
        assert dx == 0.0
        assert dy == 0.0


# =====================================================================
# 2. Polygon Deformation & Snapping Tests
# =====================================================================

class TestPolygonDeformation:
    """Tests for deforming polygon boundaries under vector displacement fields."""

    def test_apply_pure_translation_field(self, sample_cadastre_parcel: Polygon):
        # Uniform vector field shifting by dx=5.0, dy=-3.0
        vectors = [
            DisplacementVector(x=0.0, y=0.0, dx=5.0, dy=-3.0),
            DisplacementVector(x=100.0, y=100.0, dx=5.0, dy=-3.0),
        ]
        interpolator = VectorFieldInterpolator(vectors, kernel="idw")

        warped_poly = apply_vector_field_to_polygon(sample_cadastre_parcel, interpolator)

        assert warped_poly.is_valid is True
        assert pytest.approx(warped_poly.area, rel=1e-3) == sample_cadastre_parcel.area

        # Verify centroid shifted by (5.0, -3.0)
        c_orig = sample_cadastre_parcel.centroid
        c_warped = warped_poly.centroid
        assert pytest.approx(c_warped.x - c_orig.x, abs=1e-3) == 5.0
        assert pytest.approx(c_warped.y - c_orig.y, abs=1e-3) == -3.0

    def test_elastic_clamping_prevents_excessive_displacement(self, sample_cadastre_parcel: Polygon):
        # Vector with extreme shift of dx=100m, but max_displacement clamped to 15m
        vectors = [DisplacementVector(x=50.0, y=50.0, dx=100.0, dy=0.0)]
        config = DeformationConfig(max_displacement_m=15.0)
        engine = ElasticDeformationEngine(config=config)

        warped_poly, audit = engine.deform_polygon(sample_cadastre_parcel, vectors)

        assert warped_poly.is_valid is True
        assert audit.max_displacement_observed <= 15.0001
        assert audit.clamped_vertices_count > 0

    def test_deformation_preserves_valid_topology(self, sample_cadastre_parcel: Polygon):
        # Localized displacement that might pull vertices
        vectors = [
            DisplacementVector(x=0.0, y=0.0, dx=8.0, dy=8.0),
            DisplacementVector(x=100.0, y=100.0, dx=-8.0, dy=-8.0),
        ]
        config = DeformationConfig(max_displacement_m=20.0, preserve_topology=True)
        engine = ElasticDeformationEngine(config=config)

        warped_poly, audit = engine.deform_polygon(sample_cadastre_parcel, vectors)

        assert warped_poly.is_valid is True
        assert audit.topology_preserved is True
        assert warped_poly.geom_type == "Polygon"


# =====================================================================
# 3. Discrepancy-to-Field Generation Tests
# =====================================================================

class TestDisplacementFieldFromBoundaryDiscrepancies:
    """Tests for extracting displacement vectors from cadastre vs satellite boundary discrepancy pairs."""

    def test_build_displacement_field_from_shifted_satellite_boundary(
        self, sample_cadastre_parcel: Polygon
    ):
        # Satellite boundary shifted by dx=6m, dy=6m
        sat_poly = Polygon([(6.0, 6.0), (106.0, 6.0), (106.0, 106.0), (6.0, 106.0), (6.0, 6.0)])

        vectors = build_displacement_field_from_pairs(
            cadastre_geom=sample_cadastre_parcel,
            satellite_geom=sat_poly,
            sample_spacing_m=25.0,
        )

        assert len(vectors) >= 4
        # Vectors should point from cadastre towards satellite (dx ~ 6, dy ~ 6)
        for v in vectors:
            assert 5.0 <= v.dx <= 7.0
            assert 5.0 <= v.dy <= 7.0

    def test_snapping_deforms_cadastre_towards_satellite(
        self, sample_cadastre_parcel: Polygon
    ):
        sat_poly = Polygon([(4.0, 4.0), (104.0, 4.0), (104.0, 104.0), (4.0, 104.0), (4.0, 4.0)])
        engine = ElasticDeformationEngine()

        snapped_poly, audit = engine.snap_to_satellite(
            cadastre_geom=sample_cadastre_parcel,
            satellite_geom=sat_poly,
        )

        assert snapped_poly.is_valid is True
        # Area should remain roughly preserved
        assert pytest.approx(snapped_poly.area, rel=0.05) == sample_cadastre_parcel.area
        # Centroid of snapped polygon should move close to satellite centroid (54, 54)
        assert pytest.approx(snapped_poly.centroid.x, abs=1.0) == 54.0
        assert pytest.approx(snapped_poly.centroid.y, abs=1.0) == 54.0
        assert audit.rms_displacement > 0.0


# =====================================================================
# 4. Shared Boundary Concurrency & Topology Snapping Tests
# =====================================================================

class TestSharedBoundaryDeformation:
    """Tests ensuring that adjacent parcels sharing an edge deform cohesively without creating sliver gaps."""

    def test_adjacent_parcels_preserve_shared_edge(
        self, adjacent_parcel_pair: Tuple[Polygon, Polygon]
    ):
        p1, p2 = adjacent_parcel_pair
        engine = ElasticDeformationEngine(config=DeformationConfig(max_displacement_m=10.0))

        # Shared displacement along boundary (x=100) shifting east by 5m
        vectors = [
            DisplacementVector(x=100.0, y=0.0, dx=5.0, dy=0.0),
            DisplacementVector(x=100.0, y=50.0, dx=5.0, dy=0.0),
            DisplacementVector(x=100.0, y=100.0, dx=5.0, dy=0.0),
        ]

        deformed_parcels, shared_audit = engine.deform_parcel_cluster([p1, p2], vectors)
        assert len(deformed_parcels) == 2
        p1_def, p2_def = deformed_parcels[0], deformed_parcels[1]

        assert p1_def.is_valid is True
        assert p2_def.is_valid is True

        # Intersection between p1 and p2 should remain a LineString (or empty/negligible overlap), with NO polygon overlap
        intersection = p1_def.intersection(p2_def)
        assert intersection.area < 1e-4  # Zero area overlap
        assert shared_audit["topology_clean"] is True
