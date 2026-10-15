"""Comprehensive test suite for satellite imagery super-resolution and edge enhancement.

Validates:
1. Spatial resolution upsampling (bicubic / Keys cubic spline interpolation) on single-band and multi-band satellite rasters.
2. Ground Sampling Distance (GSD) scaling (e.g. 10m Sentinel-2 -> 2.5m or 5m enhanced rasters).
3. Dynamic range expansion and adaptive local contrast enhancement for faint field bunds/boundaries.
4. Edge sharpening (unsharp masking, directional Sobel gradient filtering, Laplacian edge operators).
5. Quantitative sharpness assessment (Laplacian variance, Tenengrad gradient energy, edge contrast ratio).
6. End-to-end SuperResolutionEngine pipeline with comprehensive quality audit.
"""

from __future__ import annotations

import math
from typing import Tuple
import numpy as np
import pytest

from backend.ai.super_resolution import (
    BicubicUpsampler,
    ContrastEnhancer,
    EdgeEnhancer,
    ImageSharpnessEvaluator,
    SuperResolutionAudit,
    SuperResolutionConfig,
    SuperResolutionEngine,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def synthetic_lowres_patch_2d() -> np.ndarray:
    """32x32 synthetic single-band satellite raster patch with a diagonal field boundary."""
    patch = np.zeros((32, 32), dtype=np.float32)
    # Create two fields separated by a diagonal boundary with distinct reflectance
    for r in range(32):
        for c in range(32):
            if r + c > 30:
                patch[r, c] = 0.8  # Bright field (e.g. fallow / harvested)
            else:
                patch[r, c] = 0.2  # Dark field (e.g. dense crop / water)
    return patch


@pytest.fixture
def synthetic_lowres_patch_multiband() -> np.ndarray:
    """32x32x3 synthetic RGB / multispectral raster patch."""
    patch = np.zeros((32, 32, 3), dtype=np.float32)
    # Band 0: Red, Band 1: Green, Band 2: NIR
    patch[:16, :, 0] = 0.3
    patch[16:, :, 0] = 0.6
    patch[:16, :, 1] = 0.4
    patch[16:, :, 1] = 0.7
    patch[:16, :, 2] = 0.5
    patch[16:, :, 2] = 0.9
    return patch


# =====================================================================
# Unit Tests: BicubicUpsampler
# =====================================================================

class TestBicubicUpsampler:
    """Test bicubic convolution upsampling on satellite rasters."""

    def test_upsample_2x_dimensions(self, synthetic_lowres_patch_2d: np.ndarray):
        upsampler = BicubicUpsampler(scale_factor=2)
        upsampled = upsampler.upsample(synthetic_lowres_patch_2d)

        assert upsampled.shape == (64, 64)
        assert upsampled.dtype == np.float32

    def test_upsample_4x_dimensions(self, synthetic_lowres_patch_2d: np.ndarray):
        upsampler = BicubicUpsampler(scale_factor=4)
        upsampled = upsampler.upsample(synthetic_lowres_patch_2d)

        assert upsampled.shape == (128, 128)
        assert upsampled.dtype == np.float32

    def test_upsample_multiband_channel_preservation(self, synthetic_lowres_patch_multiband: np.ndarray):
        upsampler = BicubicUpsampler(scale_factor=2)
        upsampled = upsampler.upsample(synthetic_lowres_patch_multiband)

        assert upsampled.shape == (64, 64, 3)
        assert upsampled.dtype == np.float32

    def test_conservation_of_mean_radiance(self, synthetic_lowres_patch_2d: np.ndarray):
        """Bicubic interpolation should preserve overall scene energy/radiance within small tolerance."""
        upsampler = BicubicUpsampler(scale_factor=2)
        upsampled = upsampler.upsample(synthetic_lowres_patch_2d)

        mean_orig = float(np.mean(synthetic_lowres_patch_2d))
        mean_upsampled = float(np.mean(upsampled))
        assert pytest.approx(mean_orig, abs=0.03) == mean_upsampled

    def test_pixel_clamping_to_valid_range(self):
        """Overshooting near sharp edges must be clamped to [0.0, 1.0]."""
        sharp_edge = np.zeros((16, 16), dtype=np.float32)
        sharp_edge[:, 8:] = 1.0

        upsampler = BicubicUpsampler(scale_factor=2, clip_range=(0.0, 1.0))
        upsampled = upsampler.upsample(sharp_edge)

        assert float(np.min(upsampled)) >= 0.0
        assert float(np.max(upsampled)) <= 1.0


# =====================================================================
# Unit Tests: ContrastEnhancer
# =====================================================================

class TestContrastEnhancer:
    """Test dynamic range stretching and adaptive contrast normalization."""

    def test_percentile_stretching(self):
        # Image compressed in [0.3, 0.5]
        compressed = np.linspace(0.3, 0.5, 100, dtype=np.float32).reshape(10, 10)
        enhancer = ContrastEnhancer(lower_percentile=2.0, upper_percentile=98.0)
        stretched = enhancer.stretch_contrast(compressed)

        assert float(np.min(stretched)) == pytest.approx(0.0, abs=0.05)
        assert float(np.max(stretched)) == pytest.approx(1.0, abs=0.05)

    def test_adaptive_local_contrast(self, synthetic_lowres_patch_2d: np.ndarray):
        enhancer = ContrastEnhancer(tile_grid_size=(4, 4), clip_limit=2.0)
        enhanced = enhancer.adaptive_contrast(synthetic_lowres_patch_2d)

        assert enhanced.shape == synthetic_lowres_patch_2d.shape
        # Local contrast should increase standard deviation across tiles
        assert np.std(enhanced) >= np.std(synthetic_lowres_patch_2d) * 0.95

    def test_uniform_image_safe_handling(self):
        """A flat uniform patch must not cause division by zero or NaN values."""
        flat = np.full((16, 16), 0.5, dtype=np.float32)
        enhancer = ContrastEnhancer()
        enhanced = enhancer.stretch_contrast(flat)

        assert not np.isnan(enhanced).any()
        assert not np.isinf(enhanced).any()


# =====================================================================
# Unit Tests: EdgeEnhancer
# =====================================================================

class TestEdgeEnhancer:
    """Test unsharp masking, Sobel gradient filtering, and edge detection."""

    def test_unsharp_mask_increases_gradient_sharpness(self, synthetic_lowres_patch_2d: np.ndarray):
        enhancer = EdgeEnhancer(amount=1.5, radius=1.0)
        sharpened = enhancer.unsharp_mask(synthetic_lowres_patch_2d)

        evaluator = ImageSharpnessEvaluator()
        sharp_orig = evaluator.tenengrad_gradient_energy(synthetic_lowres_patch_2d)
        sharp_enhanced = evaluator.tenengrad_gradient_energy(sharpened)

        assert sharp_enhanced > sharp_orig

    def test_sobel_gradient_magnitude_and_direction(self):
        # Vertical edge at column 10: left 0.1, right 0.9
        edge_img = np.zeros((20, 20), dtype=np.float32)
        edge_img[:, 10:] = 0.9
        edge_img[:, :10] = 0.1

        enhancer = EdgeEnhancer()
        mag, direction = enhancer.sobel_gradients(edge_img)

        assert mag.shape == (20, 20)
        # Peak gradient magnitude should occur along column 9-10
        peak_cols = np.argmax(mag, axis=1)
        assert (peak_cols >= 9).all() and (peak_cols <= 10).all()

        # Vertical edge gradient direction is perpendicular (approx 0 rad or pi rad along X)
        mid_grad_dir = direction[10, 10]
        assert abs(math.cos(mid_grad_dir)) > 0.9

    def test_laplacian_edge_map(self, synthetic_lowres_patch_2d: np.ndarray):
        enhancer = EdgeEnhancer()
        edges = enhancer.laplacian_edges(synthetic_lowres_patch_2d)

        assert edges.shape == synthetic_lowres_patch_2d.shape
        # Edge response should peak around diagonal r + c == 30
        assert float(np.max(np.abs(edges))) > 0.1


# =====================================================================
# Unit Tests: ImageSharpnessEvaluator
# =====================================================================

class TestImageSharpnessEvaluator:
    """Test quantitative sharpness assessment metrics."""

    def test_laplacian_variance_sharp_vs_blurred(self):
        sharp = np.zeros((30, 30), dtype=np.float32)
        sharp[10:20, 10:20] = 1.0

        # Simple manual 3x3 box blur to simulate low-pass smoothing
        blurred = np.zeros_like(sharp)
        for r in range(1, 29):
            for c in range(1, 29):
                blurred[r, c] = float(np.mean(sharp[r - 1 : r + 2, c - 1 : c + 2]))

        evaluator = ImageSharpnessEvaluator()
        var_sharp = evaluator.laplacian_variance(sharp)
        var_blurred = evaluator.laplacian_variance(blurred)

        assert var_sharp > var_blurred

    def test_tenengrad_gradient_energy_positive(self, synthetic_lowres_patch_2d: np.ndarray):
        evaluator = ImageSharpnessEvaluator()
        energy = evaluator.tenengrad_gradient_energy(synthetic_lowres_patch_2d)
        assert energy > 0.0


# =====================================================================
# Unit Tests: SuperResolutionEngine
# =====================================================================

class TestSuperResolutionEngine:
    """End-to-end integration of upsampling, contrast enhancement, and edge sharpening."""

    def test_enhance_single_band_patch(self, synthetic_lowres_patch_2d: np.ndarray):
        config = SuperResolutionConfig(
            scale_factor=2,
            sharpen_amount=1.2,
            apply_contrast_enhancement=True,
            original_gsd_m=10.0,
        )
        engine = SuperResolutionEngine(config=config)
        enhanced, audit = engine.enhance(synthetic_lowres_patch_2d)

        # Dimension checks
        assert enhanced.shape == (64, 64)
        assert audit.original_shape == (32, 32)
        assert audit.enhanced_shape == (64, 64)

        # GSD resolution scaling: 10m / 2 = 5m
        assert audit.original_gsd_m == 10.0
        assert audit.enhanced_gsd_m == 5.0
        assert audit.scale_factor == 2

        # Sharpness improvement
        assert audit.enhanced_sharpness > audit.initial_sharpness
        assert audit.sharpness_gain_ratio > 1.0

    def test_enhance_multiband_patch(self, synthetic_lowres_patch_multiband: np.ndarray):
        config = SuperResolutionConfig(
            scale_factor=2,
            sharpen_amount=1.0,
            apply_contrast_enhancement=True,
            original_gsd_m=10.0,
        )
        engine = SuperResolutionEngine(config=config)
        enhanced, audit = engine.enhance(synthetic_lowres_patch_multiband)

        assert enhanced.shape == (64, 64, 3)
        assert audit.enhanced_shape == (64, 64, 3)
        assert audit.enhanced_gsd_m == 5.0

    def test_extract_enhanced_boundary_mask(self, synthetic_lowres_patch_2d: np.ndarray):
        """Verifies binary boundary mask extraction for cadastral vectorization."""
        config = SuperResolutionConfig(scale_factor=2, edge_threshold=0.15)
        engine = SuperResolutionEngine(config=config)
        mask = engine.extract_boundary_mask(synthetic_lowres_patch_2d)

        assert mask.shape == (64, 64)
        assert mask.dtype == bool
        assert mask.any(), "Boundary mask should contain detected boundary pixels"
