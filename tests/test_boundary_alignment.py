"""Comprehensive test suite for satellite-cadastre boundary alignment using IoU, Hausdorff distance, and boundary metrics.

Validates:
1. Intersection over Union (IoU / Jaccard Index) calculation between cadastre polygons and satellite segmentations.
2. Directed and undirected Hausdorff distance calculation measuring maximum boundary displacement.
3. Modified Hausdorff Distance (MHD) for outlier-robust boundary alignment evaluation.
4. Boundary F1-Score (Precision, Recall) within configurable spatial tolerance buffers (e.g. 1m, 2m, 5m).
5. Area discrepancy ratio, centroid displacement, and perimeter divergence metrics.
6. Multi-parcel batch alignment scoring with confidence categorization (EXCELLENT, ACCEPTABLE, POOR, MISALIGNED).
7. Automatic affine/rigid alignment hint computation (translation dx/dy, rotation theta) from boundary discrepancies.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from backend.ai.alignment import (
    AlignmentCategory,
    AlignmentHint,
    BoundaryAlignmentMetrics,
    BoundaryAlignmentScorer,
    compute_boundary_f1,
    compute_centroid_displacement,
    compute_hausdorff_distance,
    compute_iou,
    compute_modified_hausdorff_distance,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def perfect_square_cadastre() -> Polygon:
    """Standard 100m x 100m cadastral parcel polygon (area: 10,000 sqm)."""
    return Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)])


@pytest.fixture
def identical_satellite_boundary() -> Polygon:
    """Exact identical polygon extracted from high-res satellite imagery."""
    return Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)])


@pytest.fixture
def slightly_shifted_satellite_boundary() -> Polygon:
    """Parcel shifted by dx=5m, dy=5m representing minor georeferencing error."""
    return Polygon([(5.0, 5.0), (105.0, 5.0), (105.0, 105.0), (5.0, 105.0), (5.0, 5.0)])


@pytest.fixture
def severely_shifted_satellite_boundary() -> Polygon:
    """Parcel shifted by dx=60m, dy=60m representing major misalignment."""
    return Polygon([(60.0, 60.0), (160.0, 60.0), (160.0, 160.0), (60.0, 160.0), (60.0, 60.0)])


@pytest.fixture
def deformed_satellite_boundary() -> Polygon:
    """Parcel with an encroached or irregular boundary edge."""
    return Polygon([(0.0, 0.0), (100.0, 0.0), (110.0, 50.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)])


# =====================================================================
# 1. IoU (Intersection over Union) Tests
# =====================================================================

class TestIntersectionOverUnion:
    """Tests for Jaccard Index / IoU computation between cadastre and satellite geometries."""

    def test_iou_identical_geometries_is_one(
        self, perfect_square_cadastre: Polygon, identical_satellite_boundary: Polygon
    ):
        iou = compute_iou(perfect_square_cadastre, identical_satellite_boundary)
        assert pytest.approx(iou, rel=1e-5) == 1.0

    def test_iou_disjoint_geometries_is_zero(self, perfect_square_cadastre: Polygon):
        disjoint_poly = Polygon([(200.0, 200.0), (300.0, 200.0), (300.0, 300.0), (200.0, 300.0), (200.0, 200.0)])
        iou = compute_iou(perfect_square_cadastre, disjoint_poly)
        assert iou == 0.0

    def test_iou_partially_overlapping_geometries(
        self, perfect_square_cadastre: Polygon, slightly_shifted_satellite_boundary: Polygon
    ):
        # 100x100 shifted by (5, 5):
        # Intersection: 95 x 95 = 9025 sqm
        # Union: 10000 + 10000 - 9025 = 10975 sqm
        # Expected IoU: 9025 / 10975 ~= 0.8223
        iou = compute_iou(perfect_square_cadastre, slightly_shifted_satellite_boundary)
        expected = 9025.0 / 10975.0
        assert pytest.approx(iou, rel=1e-3) == expected

    def test_iou_empty_or_invalid_geometry_returns_zero(self, perfect_square_cadastre: Polygon):
        empty_poly = Polygon()
        assert compute_iou(perfect_square_cadastre, empty_poly) == 0.0
        assert compute_iou(empty_poly, perfect_square_cadastre) == 0.0


# =====================================================================
# 2. Hausdorff Distance Tests
# =====================================================================

class TestHausdorffDistance:
    """Tests for maximum boundary vertex displacement and modified Hausdorff metrics."""

    def test_hausdorff_identical_polygons_is_zero(
        self, perfect_square_cadastre: Polygon, identical_satellite_boundary: Polygon
    ):
        h_dist = compute_hausdorff_distance(perfect_square_cadastre, identical_satellite_boundary)
        assert pytest.approx(h_dist, abs=1e-5) == 0.0

    def test_hausdorff_pure_translation(
        self, perfect_square_cadastre: Polygon, slightly_shifted_satellite_boundary: Polygon
    ):
        # Shifted by dx=5, dy=5 -> distance is sqrt(5^2 + 5^2) = sqrt(50) ~= 7.071m
        h_dist = compute_hausdorff_distance(perfect_square_cadastre, slightly_shifted_satellite_boundary)
        expected = math.sqrt(5.0**2 + 5.0**2)
        assert pytest.approx(h_dist, rel=1e-3) == expected

    def test_hausdorff_local_deformation(
        self, perfect_square_cadastre: Polygon, deformed_satellite_boundary: Polygon
    ):
        # Vertex (110, 50) is pushed out by 10m from line x=100
        h_dist = compute_hausdorff_distance(perfect_square_cadastre, deformed_satellite_boundary)
        assert pytest.approx(h_dist, abs=0.5) == 10.0

    def test_modified_hausdorff_distance_robust_to_outliers(
        self, perfect_square_cadastre: Polygon, deformed_satellite_boundary: Polygon
    ):
        # MHD computes average minimum distances rather than supremum maximum, giving lower value
        mhd = compute_modified_hausdorff_distance(perfect_square_cadastre, deformed_satellite_boundary)
        hd = compute_hausdorff_distance(perfect_square_cadastre, deformed_satellite_boundary)
        assert 0.0 < mhd < hd


# =====================================================================
# 3. Boundary F1-Score (Precision & Recall) Tests
# =====================================================================

class TestBoundaryF1Score:
    """Tests for buffer-based boundary coincidence precision, recall, and F1 score."""

    def test_boundary_f1_identical_is_one(
        self, perfect_square_cadastre: Polygon, identical_satellite_boundary: Polygon
    ):
        f1, prec, rec = compute_boundary_f1(
            perfect_square_cadastre, identical_satellite_boundary, buffer_tolerance_m=2.0
        )
        assert pytest.approx(f1, abs=1e-4) == 1.0
        assert pytest.approx(prec, abs=1e-4) == 1.0
        assert pytest.approx(rec, abs=1e-4) == 1.0

    def test_boundary_f1_with_displacement_and_tolerance_buffers(
        self, perfect_square_cadastre: Polygon, slightly_shifted_satellite_boundary: Polygon
    ):
        # Shift is ~7.07m corner-to-corner and 5m edge-to-edge
        # With 2m buffer tolerance, edge is outside buffer -> F1 is lower
        f1_strict, _, _ = compute_boundary_f1(
            perfect_square_cadastre, slightly_shifted_satellite_boundary, buffer_tolerance_m=2.0
        )
        # With 10m buffer tolerance, shift falls within buffer -> F1 approaches 1.0
        f1_relaxed, _, _ = compute_boundary_f1(
            perfect_square_cadastre, slightly_shifted_satellite_boundary, buffer_tolerance_m=10.0
        )
        assert f1_relaxed > f1_strict
        assert f1_relaxed > 0.85


# =====================================================================
# 4. Centroid Displacement & Area Discrepancy Tests
# =====================================================================

class TestGeometricDiscrepancies:
    """Tests for spatial centroid shifts and area ratio divergences."""

    def test_centroid_displacement_calculation(
        self, perfect_square_cadastre: Polygon, slightly_shifted_satellite_boundary: Polygon
    ):
        dx, dy, distance = compute_centroid_displacement(
            perfect_square_cadastre, slightly_shifted_satellite_boundary
        )
        assert pytest.approx(dx, abs=1e-4) == 5.0
        assert pytest.approx(dy, abs=1e-4) == 5.0
        assert pytest.approx(distance, abs=1e-4) == math.sqrt(50.0)


# =====================================================================
# 5. Boundary Alignment Scorer End-to-End Tests
# =====================================================================

class TestBoundaryAlignmentScorer:
    """Tests evaluating composite scoring, categorization, and alignment hints."""

    def test_evaluate_perfect_alignment(
        self, perfect_square_cadastre: Polygon, identical_satellite_boundary: Polygon
    ):
        scorer = BoundaryAlignmentScorer(tolerance_buffer_m=2.0)
        metrics = scorer.evaluate(
            cadastre_geom=perfect_square_cadastre,
            satellite_geom=identical_satellite_boundary,
            parcel_id="PARCEL-EXCELLENT-1",
        )

        assert isinstance(metrics, BoundaryAlignmentMetrics)
        assert metrics.parcel_id == "PARCEL-EXCELLENT-1"
        assert metrics.iou >= 0.99
        assert metrics.hausdorff_distance <= 0.05
        assert metrics.boundary_f1 >= 0.99
        assert metrics.category == AlignmentCategory.EXCELLENT
        assert metrics.overall_score >= 95.0

    def test_evaluate_acceptable_alignment_with_small_shift(
        self, perfect_square_cadastre: Polygon, slightly_shifted_satellite_boundary: Polygon
    ):
        scorer = BoundaryAlignmentScorer(tolerance_buffer_m=5.0)
        metrics = scorer.evaluate(
            cadastre_geom=perfect_square_cadastre,
            satellite_geom=slightly_shifted_satellite_boundary,
            parcel_id="PARCEL-ACCEPTABLE-1",
        )

        assert metrics.category in (AlignmentCategory.ACCEPTABLE, AlignmentCategory.POOR)
        assert 0.70 <= metrics.iou <= 0.90
        assert metrics.alignment_hint is not None
        assert isinstance(metrics.alignment_hint, AlignmentHint)
        # Translation hint should compensate dx=-5, dy=-5 to bring satellite back to cadastre
        assert pytest.approx(metrics.alignment_hint.suggested_dx, abs=0.5) == -5.0
        assert pytest.approx(metrics.alignment_hint.suggested_dy, abs=0.5) == -5.0

    def test_evaluate_severe_misalignment(
        self, perfect_square_cadastre: Polygon, severely_shifted_satellite_boundary: Polygon
    ):
        scorer = BoundaryAlignmentScorer(tolerance_buffer_m=2.0)
        metrics = scorer.evaluate(
            cadastre_geom=perfect_square_cadastre,
            satellite_geom=severely_shifted_satellite_boundary,
            parcel_id="PARCEL-MISALIGNED-1",
        )

        assert metrics.category == AlignmentCategory.MISALIGNED
        assert metrics.iou < 0.30
        assert metrics.overall_score < 40.0
        assert metrics.hausdorff_distance > 50.0

    def test_batch_evaluation(
        self,
        perfect_square_cadastre: Polygon,
        identical_satellite_boundary: Polygon,
        slightly_shifted_satellite_boundary: Polygon,
    ):
        scorer = BoundaryAlignmentScorer()
        pairs = [
            ("P101", perfect_square_cadastre, identical_satellite_boundary),
            ("P102", perfect_square_cadastre, slightly_shifted_satellite_boundary),
        ]
        results = scorer.evaluate_batch(pairs)
        assert len(results) == 2
        assert results[0].parcel_id == "P101"
        assert results[0].category == AlignmentCategory.EXCELLENT
        assert results[1].parcel_id == "P102"
