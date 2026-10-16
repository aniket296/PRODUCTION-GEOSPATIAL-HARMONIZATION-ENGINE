"""Super-resolution, adaptive contrast normalization, and edge enhancement for low-resolution satellite imagery.

Provides deterministic, offline-capable raster enhancement for extracting sharp parcel boundaries
from medium/low-resolution multispectral or panchromatic imagery (e.g., Sentinel-2 10m -> 2.5m/5m):
1. BicubicUpsampler: Separable Keys cubic convolution interpolation for 2D and 3D multi-band rasters.
2. ContrastEnhancer: Percentile dynamic range stretching and tile-based adaptive local contrast normalization.
3. EdgeEnhancer: Gaussian unsharp masking, directional Sobel gradient operators, and Laplacian edge extraction.
4. ImageSharpnessEvaluator: Quantitative sharpness metrics (Laplacian variance and Tenengrad gradient energy).
5. SuperResolutionEngine: End-to-end enhancement pipeline with GSD scaling and quality auditing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
import numpy as np


def _convolve2d_3x3(band: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Fast vectorized 3x3 2D convolution with edge-replicated padding."""
    padded = np.pad(band.astype(np.float32), pad_width=1, mode="edge")
    h, w = band.shape
    out = np.zeros((h, w), dtype=np.float32)
    for dr in range(3):
        for dc in range(3):
            weight = float(kernel[dr, dc])
            if weight != 0.0:
                out += weight * padded[dr : dr + h, dc : dc + w]
    return out


def _gaussian_kernel_1d(sigma: float) -> np.ndarray:
    """Generate normalized 1D Gaussian convolution kernel."""
    if sigma <= 0.0:
        return np.array([1.0], dtype=np.float32)
    radius = max(1, int(math.ceil(3.0 * sigma)))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-(x * x) / (2.0 * sigma * sigma))
    kernel_sum = float(np.sum(kernel))
    if kernel_sum > 0.0:
        kernel /= kernel_sum
    return kernel.astype(np.float32)


def _convolve1d_axis(arr: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    """Apply 1D convolution along specified axis (0 or 1) of a 2D array."""
    radius = len(kernel) // 2
    if radius == 0:
        return arr.copy()
    if axis == 0:
        padded = np.pad(arr, ((radius, radius), (0, 0)), mode="edge")
        h, w = arr.shape
        out = np.zeros((h, w), dtype=np.float32)
        for i, weight in enumerate(kernel):
            out += float(weight) * padded[i : i + h, :]
        return out
    else:
        padded = np.pad(arr, ((0, 0), (radius, radius)), mode="edge")
        h, w = arr.shape
        out = np.zeros((h, w), dtype=np.float32)
        for i, weight in enumerate(kernel):
            out += float(weight) * padded[:, i : i + w]
        return out


class BicubicUpsampler:
    """Separable Keys cubic spline upsampler for single-band and multi-band satellite rasters."""

    def __init__(
        self,
        scale_factor: int = 2,
        clip_range: Optional[Tuple[float, float]] = (0.0, 1.0),
        alpha: float = -0.5,
    ) -> None:
        if scale_factor < 1:
            raise ValueError("scale_factor must be >= 1")
        self.scale_factor = int(scale_factor)
        self.clip_range = clip_range
        self.alpha = float(alpha)

    def _cubic_weights(self, x: np.ndarray) -> np.ndarray:
        """Evaluate Keys cubic interpolation kernel W(x) for array of distances."""
        abs_x = np.abs(x)
        abs_x2 = abs_x * abs_x
        abs_x3 = abs_x2 * abs_x
        a = self.alpha

        w = np.zeros_like(abs_x, dtype=np.float32)
        mask1 = abs_x <= 1.0
        mask2 = (abs_x > 1.0) & (abs_x < 2.0)

        w[mask1] = (a + 2.0) * abs_x3[mask1] - (a + 3.0) * abs_x2[mask1] + 1.0
        w[mask2] = a * abs_x3[mask2] - 5.0 * a * abs_x2[mask2] + 8.0 * a * abs_x[mask2] - 4.0 * a
        return w

    def _upsample_1d_axis0(self, arr: np.ndarray, scale: int) -> np.ndarray:
        """Upsample along axis 0 (rows) using 4-tap cubic convolution."""
        if scale == 1:
            return arr.astype(np.float32)

        in_h = arr.shape[0]
        out_h = in_h * scale

        # Map output indices to continuous input coordinates
        out_idx = np.arange(out_h, dtype=np.float32)
        u = (out_idx + 0.5) / float(scale) - 0.5
        base_idx = np.floor(u).astype(np.int32)
        frac = u - base_idx

        # 4 neighbor offsets: -1, 0, +1, +2
        offsets = np.array([-1, 0, 1, 2], dtype=np.int32)
        # Shape: (out_h, 4)
        sample_indices = np.clip(base_idx[:, None] + offsets[None, :], 0, in_h - 1)
        weights = self._cubic_weights(frac[:, None] - offsets[None, :].astype(np.float32))
        weight_sums = np.sum(weights, axis=1, keepdims=True)
        weight_sums = np.where(np.abs(weight_sums) < 1e-8, 1.0, weight_sums)
        weights /= weight_sums

        # Gather and weight-sum along axis 0
        # arr[sample_indices] has shape (out_h, 4, W) or (out_h, 4, W, C)
        sampled = arr[sample_indices]
        if arr.ndim == 2:
            out = np.sum(sampled * weights[:, :, None], axis=1)
        else:
            out = np.sum(sampled * weights[:, :, None, None], axis=1)
        return out.astype(np.float32)

    def upsample(self, image: np.ndarray) -> np.ndarray:
        """Upsample 2D (H, W) or 3D (H, W, C) raster array by scale_factor."""
        arr = np.asarray(image, dtype=np.float32)
        if arr.ndim not in (2, 3):
            raise ValueError(f"Expected 2D or 3D array, got {arr.ndim}D")

        # Separable pass 1: rows (axis 0)
        row_up = self._upsample_1d_axis0(arr, self.scale_factor)
        # Separable pass 2: columns (swap axes 0 and 1, upsample, swap back)
        swapped = np.swapaxes(row_up, 0, 1)
        col_up = self._upsample_1d_axis0(swapped, self.scale_factor)
        result = np.swapaxes(col_up, 0, 1)

        if self.clip_range is not None:
            result = np.clip(result, self.clip_range[0], self.clip_range[1])

        return result.astype(np.float32)


class ContrastEnhancer:
    """Dynamic range expansion and tile-based adaptive contrast enhancement."""

    def __init__(
        self,
        lower_percentile: float = 2.0,
        upper_percentile: float = 98.0,
        tile_grid_size: Tuple[int, int] = (4, 4),
        clip_limit: float = 2.0,
    ) -> None:
        self.lower_percentile = float(lower_percentile)
        self.upper_percentile = float(upper_percentile)
        self.tile_grid_size = tile_grid_size
        self.clip_limit = float(clip_limit)

    def _stretch_band(self, band: np.ndarray) -> np.ndarray:
        p_low = float(np.percentile(band, self.lower_percentile))
        p_high = float(np.percentile(band, self.upper_percentile))
        span = p_high - p_low
        if span < 1e-6:
            return np.clip(band.astype(np.float32), 0.0, 1.0)
        stretched = (band.astype(np.float32) - p_low) / span
        return np.clip(stretched, 0.0, 1.0).astype(np.float32)

    def stretch_contrast(self, image: np.ndarray) -> np.ndarray:
        """Apply global percentile-based contrast stretching across [0.0, 1.0]."""
        arr = np.asarray(image, dtype=np.float32)
        if arr.ndim == 2:
            return self._stretch_band(arr)
        elif arr.ndim == 3:
            out = np.empty_like(arr, dtype=np.float32)
            for c in range(arr.shape[2]):
                out[:, :, c] = self._stretch_band(arr[:, :, c])
            return out
        else:
            raise ValueError(f"Expected 2D or 3D image array, got {arr.ndim}D")

    def _adaptive_band(self, band: np.ndarray) -> np.ndarray:
        """Tile-based local contrast normalization blended with global stretch."""
        global_stretched = self._stretch_band(band)
        h, w = band.shape
        gh, gw = self.tile_grid_size
        tile_h = max(1, h // gh)
        tile_w = max(1, w // gw)

        local_out = np.empty_like(global_stretched, dtype=np.float32)
        for r0 in range(0, h, tile_h):
            r1 = min(h, r0 + tile_h)
            for c0 in range(0, w, tile_w):
                c1 = min(w, c0 + tile_w)
                tile = band[r0:r1, c0:c1]
                t_min = float(np.min(tile))
                t_max = float(np.max(tile))
                t_span = t_max - t_min
                if t_span >= (1.0 / max(self.clip_limit, 1.0)):
                    local_out[r0:r1, c0:c1] = np.clip((tile - t_min) / t_span, 0.0, 1.0)
                else:
                    local_out[r0:r1, c0:c1] = global_stretched[r0:r1, c0:c1]

        # Blend 60% global + 40% local to avoid harsh tile seams while enhancing local contrast
        blended = 0.6 * global_stretched + 0.4 * local_out
        return np.clip(blended, 0.0, 1.0).astype(np.float32)

    def adaptive_contrast(self, image: np.ndarray) -> np.ndarray:
        """Apply adaptive tile-based contrast enhancement."""
        arr = np.asarray(image, dtype=np.float32)
        if arr.ndim == 2:
            return self._adaptive_band(arr)
        elif arr.ndim == 3:
            out = np.empty_like(arr, dtype=np.float32)
            for c in range(arr.shape[2]):
                out[:, :, c] = self._adaptive_band(arr[:, :, c])
            return out
        else:
            raise ValueError(f"Expected 2D or 3D image array, got {arr.ndim}D")


class EdgeEnhancer:
    """Unsharp masking, Sobel directional gradients, and Laplacian edge extraction."""

    SOBEL_X = np.array([[-1.0, 0.0, 1.0], [-2.0, 0.0, 2.0], [-1.0, 0.0, 1.0]], dtype=np.float32)
    SOBEL_Y = np.array([[-1.0, -2.0, -1.0], [0.0, 0.0, 0.0], [1.0, 2.0, 1.0]], dtype=np.float32)
    LAPLACIAN_KERNEL = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32)

    def __init__(self, amount: float = 1.2, radius: float = 1.0) -> None:
        self.amount = float(amount)
        self.radius = float(radius)

    def _gaussian_blur_band(self, band: np.ndarray) -> np.ndarray:
        kernel = _gaussian_kernel_1d(self.radius)
        temp = _convolve1d_axis(band, kernel, axis=0)
        return _convolve1d_axis(temp, kernel, axis=1)

    def unsharp_mask(self, image: np.ndarray, clip_range: Optional[Tuple[float, float]] = (0.0, 1.0)) -> np.ndarray:
        """Sharpen boundary transitions via unsharp masking: I_sharp = I + amount * (I - I_blur)."""
        arr = np.asarray(image, dtype=np.float32)
        if arr.ndim == 2:
            blurred = self._gaussian_blur_band(arr)
            sharpened = arr + self.amount * (arr - blurred)
        elif arr.ndim == 3:
            sharpened = np.empty_like(arr, dtype=np.float32)
            for c in range(arr.shape[2]):
                blurred = self._gaussian_blur_band(arr[:, :, c])
                sharpened[:, :, c] = arr[:, :, c] + self.amount * (arr[:, :, c] - blurred)
        else:
            raise ValueError(f"Expected 2D or 3D image array, got {arr.ndim}D")

        if clip_range is not None:
            sharpened = np.clip(sharpened, clip_range[0], clip_range[1])
        return sharpened.astype(np.float32)

    @staticmethod
    def _to_2d_luminance(image: np.ndarray) -> np.ndarray:
        arr = np.asarray(image, dtype=np.float32)
        if arr.ndim == 2:
            return arr
        elif arr.ndim == 3:
            return np.mean(arr, axis=2).astype(np.float32)
        raise ValueError(f"Expected 2D or 3D image array, got {arr.ndim}D")

    def sobel_gradients(self, image: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Compute Sobel gradient magnitude and orientation angle (in radians)."""
        lum = self._to_2d_luminance(image)
        gx = _convolve2d_3x3(lum, self.SOBEL_X)
        gy = _convolve2d_3x3(lum, self.SOBEL_Y)
        magnitude = np.hypot(gx, gy).astype(np.float32)
        direction = np.arctan2(gy, gx).astype(np.float32)
        return magnitude, direction

    def laplacian_edges(self, image: np.ndarray) -> np.ndarray:
        """Compute 2nd-order Laplacian edge response map."""
        lum = self._to_2d_luminance(image)
        return _convolve2d_3x3(lum, self.LAPLACIAN_KERNEL)


class ImageSharpnessEvaluator:
    """Quantitative image sharpness metrics for satellite boundary quality assessment."""

    def __init__(self) -> None:
        self._edge_enhancer = EdgeEnhancer()

    def laplacian_variance(self, image: np.ndarray) -> float:
        """Compute variance of the Laplacian (Pei & Lin sharpness metric)."""
        lap = self._edge_enhancer.laplacian_edges(image)
        return float(np.var(lap))

    def tenengrad_gradient_energy(self, image: np.ndarray) -> float:
        """Compute mean Sobel gradient energy (Tenengrad focus measure)."""
        mag, _ = self._edge_enhancer.sobel_gradients(image)
        return float(np.mean(mag * mag))


@dataclass
class SuperResolutionConfig:
    """Configuration for satellite raster super-resolution and edge enhancement."""

    scale_factor: int = 2
    sharpen_amount: float = 1.2
    sharpen_radius: float = 1.0
    apply_contrast_enhancement: bool = True
    lower_percentile: float = 2.0
    upper_percentile: float = 98.0
    original_gsd_m: float = 10.0
    edge_threshold: float = 0.15


@dataclass
class SuperResolutionAudit:
    """Audit metrics quantifying spatial resolution and edge sharpness gains."""

    original_shape: Tuple[int, ...]
    enhanced_shape: Tuple[int, ...]
    scale_factor: int
    original_gsd_m: float
    enhanced_gsd_m: float
    initial_sharpness: float
    enhanced_sharpness: float
    sharpness_gain_ratio: float
    mean_radiance_before: float
    mean_radiance_after: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_shape": list(self.original_shape),
            "enhanced_shape": list(self.enhanced_shape),
            "scale_factor": self.scale_factor,
            "original_gsd_m": round(self.original_gsd_m, 4),
            "enhanced_gsd_m": round(self.enhanced_gsd_m, 4),
            "initial_sharpness": round(self.initial_sharpness, 6),
            "enhanced_sharpness": round(self.enhanced_sharpness, 6),
            "sharpness_gain_ratio": round(self.sharpness_gain_ratio, 4),
            "mean_radiance_before": round(self.mean_radiance_before, 4),
            "mean_radiance_after": round(self.mean_radiance_after, 4),
        }


class SuperResolutionEngine:
    """Orchestrates bicubic super-resolution, contrast normalization, and boundary sharpening."""

    def __init__(self, config: Optional[SuperResolutionConfig] = None) -> None:
        self.config = config or SuperResolutionConfig()
        self.upsampler = BicubicUpsampler(scale_factor=self.config.scale_factor, clip_range=(0.0, 1.0))
        self.contrast_enhancer = ContrastEnhancer(
            lower_percentile=self.config.lower_percentile,
            upper_percentile=self.config.upper_percentile,
        )
        self.edge_enhancer = EdgeEnhancer(
            amount=self.config.sharpen_amount,
            radius=self.config.sharpen_radius,
        )
        self.evaluator = ImageSharpnessEvaluator()

    def enhance(self, image: np.ndarray) -> Tuple[np.ndarray, SuperResolutionAudit]:
        """Run full super-resolution, contrast stretching, and edge sharpening pipeline."""
        arr = np.asarray(image, dtype=np.float32)
        orig_shape = tuple(arr.shape)
        mean_before = float(np.mean(arr))

        # 1. Bicubic upsampling
        upsampled = self.upsampler.upsample(arr)

        # Baseline sharpness at target resolution prior to contrast & edge enhancement
        initial_sharpness = self.evaluator.tenengrad_gradient_energy(upsampled)

        # 2. Optional dynamic range contrast enhancement
        current = upsampled
        if self.config.apply_contrast_enhancement:
            current = self.contrast_enhancer.stretch_contrast(current)

        # 3. Unsharp masking for boundary edge enhancement
        if self.config.sharpen_amount > 0.0:
            current = self.edge_enhancer.unsharp_mask(current, clip_range=(0.0, 1.0))

        enhanced_sharpness = self.evaluator.tenengrad_gradient_energy(current)
        gain_ratio = (enhanced_sharpness / initial_sharpness) if initial_sharpness > 1e-9 else 1.0

        enhanced_gsd = self.config.original_gsd_m / float(self.config.scale_factor)
        mean_after = float(np.mean(current))

        audit = SuperResolutionAudit(
            original_shape=orig_shape,
            enhanced_shape=tuple(current.shape),
            scale_factor=self.config.scale_factor,
            original_gsd_m=self.config.original_gsd_m,
            enhanced_gsd_m=enhanced_gsd,
            initial_sharpness=initial_sharpness,
            enhanced_sharpness=enhanced_sharpness,
            sharpness_gain_ratio=gain_ratio,
            mean_radiance_before=mean_before,
            mean_radiance_after=mean_after,
        )

        return current, audit

    def extract_boundary_mask(self, image: np.ndarray) -> np.ndarray:
        """Enhance low-resolution patch and extract binary boundary edge mask."""
        enhanced, _ = self.enhance(image)
        mag, _ = self.edge_enhancer.sobel_gradients(enhanced)
        # Normalize gradient magnitude to [0, 1] if max > 0
        max_mag = float(np.max(mag))
        if max_mag > 1e-8:
            norm_mag = mag / max_mag
        else:
            norm_mag = mag
        return (norm_mag >= self.config.edge_threshold).astype(bool)
