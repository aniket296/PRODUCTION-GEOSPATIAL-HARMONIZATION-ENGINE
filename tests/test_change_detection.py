"""Comprehensive test suite for multi-temporal change detection and cadastral encroachment identification.

Validates:
1. Spectral index calculation (NDVI, NDBI) with zero-denominator safety.
2. Multi-temporal Change Vector Analysis (CVA) between T1 (baseline) and T2 (current) satellite rasters.
3. Geometric boundary encroachment detection (occupied footprint vs legal cadastral parcel with buffer tolerance).
4. Encroachment severity classification (NONE, MINOR, MODERATE, CRITICAL) and lateral intrusion depth measurement.
5. Public Right-of-Way (road/canal corridor) and neighbor parcel spillover attribution.
6. End-to-end MultiTemporalChangeEngine pipeline combining raster change masks with cadastral polygons.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Polygon

from backend.ai.change_detection import (
    ChangeDetectionConfig,
    ChangeDetectionReport,
    EncroachmentAnalysisResult,
    EncroachmentDetector,
    EncroachmentSeverity,
    MultiTemporalChangeEngine,
    SpectralIndexCalculator,
    TemporalChangeResult,
    TemporalRasterDifferencer,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def legal_cadastre_parcel() -> Polygon:
    """100m x 100m (10,000 sqm) legal cadastral parcel [0..100, 0..100]."""
    return Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)])


@pytest.fixture
def adjacent_road_corridor() -> Polygon:
    """20m wide public road corridor immediately east of the parcel [100..120, 0..100]."""
    return Polygon([(100.0, 0.0), (120.0, 0.0), (120.0, 100.0), (100.0, 100.0), (100.0, 0.0)])


@pytest.fixture
def temporal_raster_pair() -> tuple[np.ndarray, np.ndarray]:
    """32x32x3 (Red, NIR, SWIR) rasters at T1 (vegetated) and T2 (new built-up construction in quadrant)."""
    # T1: healthy vegetation across entire 32x32 patch (low Red=0.1, high NIR=0.7, low SWIR=0.15)
    t1 = np.zeros((32, 32, 3), dtype=np.float32)
    t1[:, :, 0] = 0.10  # Red
    t1[:, :, 1] = 0.70  # NIR
    t1[:, :, 2] = 0.15  # SWIR

    # T2: region [10:26, 12:28] converted to concrete/built-up structure (high Red=0.45, low NIR=0.20, high SWIR=0.55)
    t2 = t1.copy()
    t2[10:26, 12:28, 0] = 0.45
    t2[10:26, 12:28, 1] = 0.20
    t2[10:26, 12:28, 2] = 0.55
    return t1, t2


# =====================================================================
# Unit Tests: SpectralIndexCalculator
# =====================================================================

class TestSpectralIndexCalculator:
    """Test NDVI and NDBI spectral index calculations."""

    def test_ndvi_computation(self):
        red = np.array([[0.1, 0.4], [0.0, 0.5]], dtype=np.float32)
        nir = np.array([[0.7, 0.2], [0.0, 0.5]], dtype=np.float32)

        ndvi = SpectralIndexCalculator.compute_ndvi(red=red, nir=nir)

        assert ndvi.shape == (2, 2)
        assert not np.isnan(ndvi).any()
        # (0.7 - 0.1) / (0.7 + 0.1) = 0.6 / 0.8 = 0.75
        assert pytest.approx(0.75, abs=1e-4) == float(ndvi[0, 0])
        # (0.2 - 0.4) / (0.2 + 0.4) = -0.2 / 0.6 = -0.3333
        assert pytest.approx(-1.0 / 3.0, abs=1e-4) == float(ndvi[0, 1])
        # Zero denominator (0, 0) safely yields 0.0
        assert float(ndvi[1, 0]) == 0.0

    def test_ndbi_computation(self):
        nir = np.array([[0.7, 0.2]], dtype=np.float32)
        swir = np.array([[0.1, 0.6]], dtype=np.float32)

        ndbi = SpectralIndexCalculator.compute_ndbi(nir=nir, swir=swir)

        assert ndbi.shape == (1, 2)
        # Vegetated pixel has negative NDBI; built-up pixel has positive NDBI
        assert float(ndbi[0, 0]) < 0.0
        assert float(ndbi[0, 1]) > 0.0


# =====================================================================
# Unit Tests: TemporalRasterDifferencer
# =====================================================================

class TestTemporalRasterDifferencer:
    """Test multi-temporal Change Vector Analysis (CVA) and land-cover transition masks."""

    def test_detect_builtup_expansion(self, temporal_raster_pair: tuple[np.ndarray, np.ndarray]):
        t1, t2 = temporal_raster_pair
        differencer = TemporalRasterDifferencer(change_magnitude_threshold=0.25)
        result: TemporalChangeResult = differencer.analyze_pair(t1, t2)

        assert result.change_mask.shape == (32, 32)
        assert result.builtup_expansion_mask.shape == (32, 32)

        # The changed block is [10:26, 12:28] -> 16 * 16 = 256 pixels out of 1024
        assert result.changed_pixel_count == 256
        assert pytest.approx(256.0 / 1024.0, abs=1e-4) == result.changed_fraction
        assert result.builtup_expansion_mask[15, 20] == True
        assert result.builtup_expansion_mask[2, 2] == False
        assert result.mean_ndvi_delta < 0.0
        assert result.mean_ndbi_delta > 0.0


# =====================================================================
# Unit Tests: EncroachmentDetector
# =====================================================================

class TestEncroachmentDetector:
    """Test geometric boundary spillover and encroachment classification."""

    def test_no_encroachment_within_tolerance(self, legal_cadastre_parcel: Polygon):
        # Occupied footprint has a 0.3m jitter outside the 100m boundary, within 0.5m tolerance
        occupied = Polygon([(0.0, 0.0), (100.3, 0.0), (100.3, 100.0), (0.0, 100.0), (0.0, 0.0)])
        detector = EncroachmentDetector(buffer_tolerance_m=0.5, min_encroachment_sqm=5.0)
        res: EncroachmentAnalysisResult = detector.evaluate_parcel(
            parcel_id="KHASRA-101",
            cadastre_poly=legal_cadastre_parcel,
            occupied_poly=occupied,
        )

        assert res.has_encroachment is False
        assert res.severity == EncroachmentSeverity.NONE
        assert res.encroached_area_sqm == 0.0

    def test_significant_boundary_encroachment_and_road_intrusion(
        self,
        legal_cadastre_parcel: Polygon,
        adjacent_road_corridor: Polygon,
    ):
        # Occupied footprint extends 12m east into the public road corridor along a 50m front (y=20..70)
        # Spillover rectangle is [100..112, 20..70] -> 12m x 50m = 600 sqm
        occupied = Polygon([
            (0.0, 0.0), (100.0, 0.0), (100.0, 20.0),
            (112.0, 20.0), (112.0, 70.0), (100.0, 70.0),
            (100.0, 100.0), (0.0, 100.0), (0.0, 0.0),
        ])

        detector = EncroachmentDetector(buffer_tolerance_m=0.0, min_encroachment_sqm=10.0)
        res = detector.evaluate_parcel(
            parcel_id="KHASRA-102",
            cadastre_poly=legal_cadastre_parcel,
            occupied_poly=occupied,
            public_corridors={"NH-ROW-01": adjacent_road_corridor},
        )

        assert res.has_encroachment is True
        assert pytest.approx(600.0, rel=0.01) == res.encroached_area_sqm
        assert pytest.approx(6.0, rel=0.01) == res.encroachment_percentage
        assert pytest.approx(12.0, rel=0.02) == res.max_lateral_intrusion_m
        assert res.severity in (EncroachmentSeverity.MODERATE, EncroachmentSeverity.CRITICAL)
        assert "NH-ROW-01" in res.affected_corridor_ids
        assert pytest.approx(600.0, rel=0.01) == res.public_corridor_intrusion_sqm


# =====================================================================
# Unit Tests: MultiTemporalChangeEngine
# =====================================================================

class TestMultiTemporalChangeEngine:
    """Test end-to-end multi-temporal raster + cadastral encroachment pipeline."""

    def test_end_to_end_temporal_encroachment_detection(
        self, temporal_raster_pair: tuple[np.ndarray, np.ndarray]
    ):
        t1, t2 = temporal_raster_pair
        # Raster is 32x32 at 2.0m GSD -> covers [0..64m, 0..64m] (with origin_y=64.0, north-up)
        # Changed built-up block in raster is rows [10:26] (y = 44..12), cols [12:28] (x = 24..56)
        # Legal cadastral parcel only covers x = [0..40], y = [0..64]
        # So the new construction spills over from x=40 to x=56 (16m wide x 32m high = 512 sqm encroachment!)
        cadastre_parcel = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 64.0), (0.0, 64.0), (0.0, 0.0)])

        config = ChangeDetectionConfig(
            origin_x=0.0,
            origin_y=64.0,
            gsd_m=2.0,
            change_magnitude_threshold=0.25,
            buffer_tolerance_m=0.0,
            min_encroachment_sqm=20.0,
        )
        engine = MultiTemporalChangeEngine(config=config)
        report: ChangeDetectionReport = engine.analyze_parcel_change(
            parcel_id="KHASRA-TEMPORAL-01",
            cadastre_poly=cadastre_parcel,
            t1_raster=t1,
            t2_raster=t2,
        )

        assert report.parcel_id == "KHASRA-TEMPORAL-01"
        assert report.temporal_change.changed_pixel_count == 256
        assert report.encroachment.has_encroachment is True
        assert pytest.approx(512.0, rel=0.05) == report.encroachment.encroached_area_sqm
        assert pytest.approx(16.0, rel=0.05) == report.encroachment.max_lateral_intrusion_m
        assert len(report.new_footprint_polygons) == 1
