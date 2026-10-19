"""Multi-temporal change detection and cadastral encroachment identification.

Provides deterministic, offline-capable analysis of satellite imagery pairs against legal cadastral boundaries:
1. SpectralIndexCalculator: NDVI / NDBI spectral indices with zero-denominator safety.
2. TemporalRasterDifferencer: Change Vector Analysis (CVA) between baseline (T1) and current (T2) rasters,
   isolating built-up expansion pixels (vegetation loss + built-up gain).
3. EncroachmentDetector: Geometric spillover of occupied footprints beyond the (tolerance-buffered) legal parcel,
   with severity classification, lateral intrusion depth and public corridor (right-of-way) attribution.
4. MultiTemporalChangeEngine: End-to-end pipeline from raster pair to georeferenced footprints to encroachment report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

from backend.ai.vectorization import PolygonVectorizationEngine, VectorizationConfig

_GEOM_EPS_SQM = 1e-6


class SpectralIndexCalculator:
    """Normalized-difference spectral index computations."""

    @staticmethod
    def _normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Compute (a - b) / (a + b), returning 0.0 where the denominator vanishes."""
        a32 = np.asarray(a, dtype=np.float32)
        b32 = np.asarray(b, dtype=np.float32)
        denom = a32 + b32
        safe = np.abs(denom) > 1e-9
        out = np.zeros_like(denom, dtype=np.float32)
        np.divide(a32 - b32, denom, out=out, where=safe)
        return out

    @staticmethod
    def compute_ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
        """NDVI = (NIR - Red) / (NIR + Red)."""
        return SpectralIndexCalculator._normalized_difference(nir, red)

    @staticmethod
    def compute_ndbi(nir: np.ndarray, swir: np.ndarray) -> np.ndarray:
        """NDBI = (SWIR - NIR) / (SWIR + NIR)."""
        return SpectralIndexCalculator._normalized_difference(swir, nir)


@dataclass
class TemporalChangeResult:
    """Outcome of multi-temporal raster differencing."""

    change_mask: np.ndarray
    builtup_expansion_mask: np.ndarray
    change_magnitude: np.ndarray
    changed_pixel_count: int
    changed_fraction: float
    builtup_pixel_count: int
    mean_ndvi_delta: float
    mean_ndbi_delta: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "changed_pixel_count": self.changed_pixel_count,
            "changed_fraction": round(self.changed_fraction, 6),
            "builtup_pixel_count": self.builtup_pixel_count,
            "mean_ndvi_delta": round(self.mean_ndvi_delta, 4),
            "mean_ndbi_delta": round(self.mean_ndbi_delta, 4),
        }


class TemporalRasterDifferencer:
    """Change Vector Analysis over (Red, NIR, SWIR) raster pairs."""

    def __init__(self, change_magnitude_threshold: float = 0.25, min_builtup_ndbi: float = -0.05) -> None:
        if change_magnitude_threshold <= 0.0:
            raise ValueError("change_magnitude_threshold must be > 0")
        self.change_magnitude_threshold = float(change_magnitude_threshold)
        self.min_builtup_ndbi = float(min_builtup_ndbi)

    @staticmethod
    def _validate_pair(t1: np.ndarray, t2: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        a = np.asarray(t1, dtype=np.float32)
        b = np.asarray(t2, dtype=np.float32)
        if a.ndim != 3 or a.shape[2] < 3:
            raise ValueError("Rasters must be 3D (H, W, >=3) with bands ordered (Red, NIR, SWIR)")
        if a.shape != b.shape:
            raise ValueError(f"Raster shape mismatch: {a.shape} vs {b.shape}")
        return a, b

    def analyze_pair(self, t1_raster: np.ndarray, t2_raster: np.ndarray) -> TemporalChangeResult:
        t1, t2 = self._validate_pair(t1_raster, t2_raster)

        diff = t2[:, :, :3] - t1[:, :, :3]
        magnitude = np.sqrt(np.sum(diff * diff, axis=2)).astype(np.float32)
        change_mask = magnitude >= self.change_magnitude_threshold

        ndvi1 = SpectralIndexCalculator.compute_ndvi(t1[:, :, 0], t1[:, :, 1])
        ndvi2 = SpectralIndexCalculator.compute_ndvi(t2[:, :, 0], t2[:, :, 1])
        ndbi1 = SpectralIndexCalculator.compute_ndbi(t1[:, :, 1], t1[:, :, 2])
        ndbi2 = SpectralIndexCalculator.compute_ndbi(t2[:, :, 1], t2[:, :, 2])
        ndvi_delta = ndvi2 - ndvi1
        ndbi_delta = ndbi2 - ndbi1

        builtup_mask = (
            change_mask
            & (ndbi_delta > 0.0)
            & (ndvi_delta < 0.0)
            & (ndbi2 >= self.min_builtup_ndbi)
        )

        changed_count = int(np.count_nonzero(change_mask))
        total = int(change_mask.size)
        if changed_count > 0:
            mean_ndvi = float(np.mean(ndvi_delta[change_mask]))
            mean_ndbi = float(np.mean(ndbi_delta[change_mask]))
        else:
            mean_ndvi = 0.0
            mean_ndbi = 0.0

        return TemporalChangeResult(
            change_mask=change_mask,
            builtup_expansion_mask=builtup_mask,
            change_magnitude=magnitude,
            changed_pixel_count=changed_count,
            changed_fraction=(changed_count / total) if total > 0 else 0.0,
            builtup_pixel_count=int(np.count_nonzero(builtup_mask)),
            mean_ndvi_delta=mean_ndvi,
            mean_ndbi_delta=mean_ndbi,
        )


class EncroachmentSeverity(str, Enum):
    """Severity classification of a boundary encroachment."""

    NONE = "NONE"
    MINOR = "MINOR"
    MODERATE = "MODERATE"
    CRITICAL = "CRITICAL"


@dataclass
class EncroachmentAnalysisResult:
    """Geometric encroachment assessment for one parcel."""

    parcel_id: str
    has_encroachment: bool
    severity: EncroachmentSeverity
    encroached_area_sqm: float
    encroachment_percentage: float
    max_lateral_intrusion_m: float
    public_corridor_intrusion_sqm: float = 0.0
    affected_corridor_ids: List[str] = field(default_factory=list)
    encroached_geometry: Optional[BaseGeometry] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "has_encroachment": self.has_encroachment,
            "severity": self.severity.value,
            "encroached_area_sqm": round(self.encroached_area_sqm, 2),
            "encroachment_percentage": round(self.encroachment_percentage, 4),
            "max_lateral_intrusion_m": round(self.max_lateral_intrusion_m, 3),
            "public_corridor_intrusion_sqm": round(self.public_corridor_intrusion_sqm, 2),
            "affected_corridor_ids": list(self.affected_corridor_ids),
        }


def _as_valid(geom: BaseGeometry) -> BaseGeometry:
    return geom if geom.is_valid else make_valid(geom)


def _vertices(geom: BaseGeometry) -> List[Tuple[float, float]]:
    """Collect all vertex coordinates of (multi)polygonal geometry, including holes."""
    pts: List[Tuple[float, float]] = []
    if geom.is_empty:
        return pts
    parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
    for part in parts:
        if isinstance(part, Polygon):
            pts.extend((float(x), float(y)) for x, y in part.exterior.coords)
            for ring in part.interiors:
                pts.extend((float(x), float(y)) for x, y in ring.coords)
        elif hasattr(part, "coords"):
            pts.extend((float(c[0]), float(c[1])) for c in part.coords)
    return pts


class EncroachmentDetector:
    """Detects occupied-footprint spillover beyond the legal cadastral parcel."""

    def __init__(
        self,
        buffer_tolerance_m: float = 0.5,
        min_encroachment_sqm: float = 5.0,
        minor_pct_threshold: float = 2.0,
        critical_pct_threshold: float = 10.0,
    ) -> None:
        self.buffer_tolerance_m = max(0.0, float(buffer_tolerance_m))
        self.min_encroachment_sqm = max(0.0, float(min_encroachment_sqm))
        self.minor_pct_threshold = float(minor_pct_threshold)
        self.critical_pct_threshold = float(critical_pct_threshold)

    def _classify(self, pct: float, corridor_sqm: float) -> EncroachmentSeverity:
        if pct >= self.critical_pct_threshold:
            severity = EncroachmentSeverity.CRITICAL
        elif pct >= self.minor_pct_threshold:
            severity = EncroachmentSeverity.MODERATE
        else:
            severity = EncroachmentSeverity.MINOR
        # Intrusion into public right-of-way is never merely minor.
        if corridor_sqm > _GEOM_EPS_SQM and severity == EncroachmentSeverity.MINOR:
            severity = EncroachmentSeverity.MODERATE
        return severity

    def evaluate_parcel(
        self,
        parcel_id: str,
        cadastre_poly: BaseGeometry,
        occupied_poly: BaseGeometry,
        public_corridors: Optional[Mapping[str, BaseGeometry]] = None,
    ) -> EncroachmentAnalysisResult:
        cadastre = _as_valid(cadastre_poly)
        occupied = _as_valid(occupied_poly)

        cadastre_area = float(cadastre.area)
        allowed = cadastre.buffer(self.buffer_tolerance_m) if self.buffer_tolerance_m > 0.0 else cadastre
        spill = occupied.difference(allowed) if not occupied.is_empty else occupied
        spill_area = float(spill.area)

        if spill.is_empty or spill_area < max(self.min_encroachment_sqm, _GEOM_EPS_SQM):
            return EncroachmentAnalysisResult(
                parcel_id=parcel_id,
                has_encroachment=False,
                severity=EncroachmentSeverity.NONE,
                encroached_area_sqm=0.0,
                encroachment_percentage=0.0,
                max_lateral_intrusion_m=0.0,
            )

        pct = (spill_area / cadastre_area * 100.0) if cadastre_area > 0.0 else 100.0
        max_intrusion = max(
            (float(cadastre.distance(Point(x, y))) for x, y in _vertices(spill)),
            default=0.0,
        )

        corridor_sqm = 0.0
        corridor_ids: List[str] = []
        for corridor_id, corridor_geom in (public_corridors or {}).items():
            overlap_area = float(spill.intersection(_as_valid(corridor_geom)).area)
            if overlap_area > _GEOM_EPS_SQM:
                corridor_sqm += overlap_area
                corridor_ids.append(corridor_id)

        return EncroachmentAnalysisResult(
            parcel_id=parcel_id,
            has_encroachment=True,
            severity=self._classify(pct, corridor_sqm),
            encroached_area_sqm=spill_area,
            encroachment_percentage=pct,
            max_lateral_intrusion_m=max_intrusion,
            public_corridor_intrusion_sqm=corridor_sqm,
            affected_corridor_ids=corridor_ids,
            encroached_geometry=spill,
        )


@dataclass
class ChangeDetectionConfig:
    """Configuration for the multi-temporal change and encroachment pipeline."""

    origin_x: float = 0.0
    origin_y: float = 0.0
    gsd_m: float = 10.0
    change_magnitude_threshold: float = 0.25
    buffer_tolerance_m: float = 0.5
    min_encroachment_sqm: float = 5.0
    min_footprint_sqm: float = 4.0
    connectivity: int = 4


@dataclass
class ChangeDetectionReport:
    """Per-parcel multi-temporal change and encroachment report."""

    parcel_id: str
    temporal_change: TemporalChangeResult
    encroachment: EncroachmentAnalysisResult
    new_footprint_polygons: List[Polygon]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "temporal_change": self.temporal_change.to_dict(),
            "encroachment": self.encroachment.to_dict(),
            "new_footprint_count": len(self.new_footprint_polygons),
        }


class MultiTemporalChangeEngine:
    """Raster pair -> built-up footprints -> cadastral encroachment report."""

    def __init__(
        self,
        config: Optional[ChangeDetectionConfig] = None,
        public_corridors: Optional[Mapping[str, BaseGeometry]] = None,
    ) -> None:
        self.config = config or ChangeDetectionConfig()
        self.public_corridors = dict(public_corridors or {})
        self.differencer = TemporalRasterDifferencer(
            change_magnitude_threshold=self.config.change_magnitude_threshold
        )
        self.detector = EncroachmentDetector(
            buffer_tolerance_m=self.config.buffer_tolerance_m,
            min_encroachment_sqm=self.config.min_encroachment_sqm,
        )
        self.vectorizer = PolygonVectorizationEngine(
            VectorizationConfig(
                origin_x=self.config.origin_x,
                origin_y=self.config.origin_y,
                gsd_m=self.config.gsd_m,
                simplify_tolerance_m=0.0,
                min_area_sqm=self.config.min_footprint_sqm,
                connectivity=self.config.connectivity,
            )
        )

    def extract_new_footprints(self, builtup_mask: np.ndarray) -> List[Polygon]:
        """Vectorize built-up expansion pixels into georeferenced footprint polygons."""
        if not np.any(builtup_mask):
            return []
        parcels, _ = self.vectorizer.vectorize_foreground_mask(builtup_mask)
        return [p.polygon for p in parcels]

    def analyze_parcel_change(
        self,
        parcel_id: str,
        cadastre_poly: BaseGeometry,
        t1_raster: np.ndarray,
        t2_raster: np.ndarray,
    ) -> ChangeDetectionReport:
        temporal = self.differencer.analyze_pair(t1_raster, t2_raster)
        footprints = self.extract_new_footprints(temporal.builtup_expansion_mask)

        # Attribute only footprints that touch this parcel (within tolerance) to its report.
        reach = _as_valid(cadastre_poly).buffer(self.config.buffer_tolerance_m)
        attributed = [fp for fp in footprints if fp.intersects(reach)]

        if attributed:
            occupied: BaseGeometry = unary_union(attributed)
            encroachment = self.detector.evaluate_parcel(
                parcel_id, cadastre_poly, occupied, public_corridors=self.public_corridors
            )
        else:
            encroachment = EncroachmentAnalysisResult(
                parcel_id=parcel_id,
                has_encroachment=False,
                severity=EncroachmentSeverity.NONE,
                encroached_area_sqm=0.0,
                encroachment_percentage=0.0,
                max_lateral_intrusion_m=0.0,
            )

        return ChangeDetectionReport(
            parcel_id=parcel_id,
            temporal_change=temporal,
            encroachment=encroachment,
            new_footprint_polygons=attributed,
        )
