"""Automated polygon vectorization and Douglas-Peucker boundary simplification from raster masks.

Provides deterministic raster-to-vector conversion for satellite boundary masks:
1. ConnectedComponentExtractor: 4/8-connected component labeling via breadth-first scanline traversal and speckle noise filtering.
2. AffineGeoTransform: Pixel (col, row) to projected world coordinate (x, y) transformation.
3. BoundarySimplifier: Topology-preserving Douglas-Peucker simplification removing raster staircase aliasing.
4. PolygonVectorizationEngine: End-to-end foreground and boundary edge mask vectorization with quality auditing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union
from shapely.validation import make_valid


@dataclass
class ConnectedComponent:
    """Discrete connected pixel region extracted from a binary mask."""

    component_id: int
    pixel_count: int
    bbox_pixels: Tuple[int, int, int, int]  # (min_row, min_col, max_row_excl, max_col_excl)
    runs: List[Tuple[int, int, int]]  # List of (row, col_start, col_end_excl)
    touches_border: bool = False


class ConnectedComponentExtractor:
    """Extracts connected foreground components from binary 2D masks and filters noise speckles."""

    def __init__(self, min_pixels: int = 1, connectivity: int = 4) -> None:
        if connectivity not in (4, 8):
            raise ValueError("connectivity must be 4 or 8")
        self.min_pixels = max(1, int(min_pixels))
        self.connectivity = connectivity

    def extract_components_with_stats(
        self, binary_mask: np.ndarray
    ) -> Tuple[List[ConnectedComponent], int]:
        """Extract connected components and return (valid_components, filtered_noise_count)."""
        mask = np.asarray(binary_mask, dtype=bool)
        if mask.ndim != 2:
            raise ValueError(f"Expected 2D binary mask, got {mask.ndim}D")

        h, w = mask.shape
        visited = np.zeros((h, w), dtype=bool)
        components: List[ConnectedComponent] = []
        filtered_noise_count = 0
        comp_id = 0

        if self.connectivity == 4:
            neighbors = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        else:
            neighbors = [
                (-1, -1), (-1, 0), (-1, 1),
                (0, -1),           (0, 1),
                (1, -1),  (1, 0),  (1, 1),
            ]

        for r0 in range(h):
            for c0 in range(w):
                if not mask[r0, c0] or visited[r0, c0]:
                    continue

                # BFS traversal
                queue: deque[Tuple[int, int]] = deque([(r0, c0)])
                visited[r0, c0] = True
                pixels: List[Tuple[int, int]] = []
                min_r, max_r = r0, r0
                min_c, max_c = c0, c0
                touches_border = False

                while queue:
                    r, c = queue.popleft()
                    pixels.append((r, c))
                    if r < min_r:
                        min_r = r
                    if r > max_r:
                        max_r = r
                    if c < min_c:
                        min_c = c
                    if c > max_c:
                        max_c = c
                    if r == 0 or r == h - 1 or c == 0 or c == w - 1:
                        touches_border = True

                    for dr, dc in neighbors:
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < h and 0 <= nc < w and mask[nr, nc] and not visited[nr, nc]:
                            visited[nr, nc] = True
                            queue.append((nr, nc))

                pixel_count = len(pixels)
                if pixel_count < self.min_pixels:
                    filtered_noise_count += 1
                    continue

                # Build horizontal scanline runs for fast exact polygon construction
                pixels.sort()
                runs: List[Tuple[int, int, int]] = []
                if pixels:
                    curr_r, curr_c_start = pixels[0]
                    curr_c_end = curr_c_start + 1
                    for r, c in pixels[1:]:
                        if r == curr_r and c == curr_c_end:
                            curr_c_end = c + 1
                        else:
                            runs.append((curr_r, curr_c_start, curr_c_end))
                            curr_r, curr_c_start = r, c
                            curr_c_end = c + 1
                    runs.append((curr_r, curr_c_start, curr_c_end))

                comp_id += 1
                components.append(
                    ConnectedComponent(
                        component_id=comp_id,
                        pixel_count=pixel_count,
                        bbox_pixels=(min_r, min_c, max_r + 1, max_c + 1),
                        runs=runs,
                        touches_border=touches_border,
                    )
                )

        return components, filtered_noise_count

    def extract_components(self, binary_mask: np.ndarray) -> List[ConnectedComponent]:
        """Extract connected components with pixel_count >= min_pixels."""
        components, _ = self.extract_components_with_stats(binary_mask)
        return components


@dataclass
class AffineGeoTransform:
    """Affine georeferencing transform mapping raster (col, row) to world (x, y)."""

    origin_x: float = 0.0
    origin_y: float = 0.0
    gsd_x: float = 1.0
    gsd_y: float = -1.0

    def pixel_to_world(self, col: float, row: float) -> Tuple[float, float]:
        """Convert fractional or integer pixel coordinate (col, row) to world (x, y)."""
        x = self.origin_x + float(col) * self.gsd_x
        y = self.origin_y + float(row) * self.gsd_y
        return x, y


class BoundarySimplifier:
    """Topology-preserving Douglas-Peucker boundary simplifier for raster contours."""

    def __init__(self, tolerance_m: float = 1.0, preserve_topology: bool = True) -> None:
        self.tolerance_m = max(0.0, float(tolerance_m))
        self.preserve_topology = bool(preserve_topology)

    def simplify(self, polygon: Polygon) -> Polygon:
        """Simplify a Shapely Polygon while preserving topological validity."""
        if self.tolerance_m <= 0.0:
            return polygon

        simplified = polygon.simplify(self.tolerance_m, preserve_topology=self.preserve_topology)
        if not simplified.is_valid:
            simplified = make_valid(simplified)

        if isinstance(simplified, Polygon) and not simplified.is_empty:
            return simplified
        elif isinstance(simplified, MultiPolygon) and len(simplified.geoms) > 0:
            return max(simplified.geoms, key=lambda g: g.area)
        return polygon


@dataclass
class VectorizationConfig:
    """Configuration parameters for automated polygon vectorization."""

    origin_x: float = 0.0
    origin_y: float = 0.0
    gsd_m: float = 1.0
    simplify_tolerance_m: float = 1.0
    min_area_sqm: float = 25.0
    max_area_sqm: Optional[float] = None
    preserve_topology: bool = True
    connectivity: int = 4


@dataclass
class VectorizedParcel:
    """Vectorized cadastral/satellite parcel polygon with geometric metadata."""

    parcel_id: str
    polygon: Polygon
    area_sqm: float
    perimeter_m: float
    vertex_count_raw: int
    vertex_count_simplified: int
    pixel_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "area_sqm": round(self.area_sqm, 2),
            "perimeter_m": round(self.perimeter_m, 2),
            "vertex_count_raw": self.vertex_count_raw,
            "vertex_count_simplified": self.vertex_count_simplified,
            "pixel_count": self.pixel_count,
        }


@dataclass
class VectorizationAudit:
    """Audit summary of a raster-to-vector polygon extraction run."""

    total_parcels_extracted: int
    noise_components_filtered: int
    oversized_components_filtered: int
    total_area_sqm: float
    all_geometries_valid: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_parcels_extracted": self.total_parcels_extracted,
            "noise_components_filtered": self.noise_components_filtered,
            "oversized_components_filtered": self.oversized_components_filtered,
            "total_area_sqm": round(self.total_area_sqm, 2),
            "all_geometries_valid": self.all_geometries_valid,
        }


class PolygonVectorizationEngine:
    """Converts binary foreground or boundary edge masks into simplified vector polygons."""

    def __init__(self, config: Optional[VectorizationConfig] = None) -> None:
        self.config = config or VectorizationConfig()
        self.transform = AffineGeoTransform(
            origin_x=self.config.origin_x,
            origin_y=self.config.origin_y,
            gsd_x=self.config.gsd_m,
            gsd_y=-self.config.gsd_m,
        )
        self.simplifier = BoundarySimplifier(
            tolerance_m=self.config.simplify_tolerance_m,
            preserve_topology=self.config.preserve_topology,
        )

    def _component_to_raw_polygon(self, comp: ConnectedComponent) -> Polygon:
        """Build exact staircase polygon from scanline runs in georeferenced world space."""
        run_boxes: List[Polygon] = []
        for r, c_start, c_end in comp.runs:
            x0, y0 = self.transform.pixel_to_world(c_start, r)
            x1, y1 = self.transform.pixel_to_world(c_end, r + 1)
            min_x, max_x = min(x0, x1), max(x0, x1)
            min_y, max_y = min(y0, y1), max(y0, y1)
            run_boxes.append(box(min_x, min_y, max_x, max_y))

        merged = unary_union(run_boxes)
        if not merged.is_valid:
            merged = make_valid(merged)

        if isinstance(merged, Polygon):
            return merged
        elif isinstance(merged, MultiPolygon) and len(merged.geoms) > 0:
            return max(merged.geoms, key=lambda g: g.area)
        raise ValueError(f"Failed to construct valid Polygon for component {comp.component_id}")

    def vectorize_foreground_mask(
        self, foreground_mask: np.ndarray, exclude_border_components: bool = False
    ) -> Tuple[List[VectorizedParcel], VectorizationAudit]:
        """Extract and simplify polygons from a binary foreground parcel mask."""
        pixel_area_sqm = self.config.gsd_m * self.config.gsd_m
        min_pixels = max(1, int(np.ceil(self.config.min_area_sqm / max(pixel_area_sqm, 1e-6))))

        extractor = ConnectedComponentExtractor(
            min_pixels=min_pixels,
            connectivity=self.config.connectivity,
        )
        components, noise_filtered = extractor.extract_components_with_stats(foreground_mask)

        parcels: List[VectorizedParcel] = []
        oversized_filtered = 0

        for comp in components:
            if exclude_border_components and comp.touches_border:
                oversized_filtered += 1
                continue

            raw_poly = self._component_to_raw_polygon(comp)
            raw_area = float(raw_poly.area)

            if raw_area < self.config.min_area_sqm:
                noise_filtered += 1
                continue

            if self.config.max_area_sqm is not None and raw_area > self.config.max_area_sqm:
                oversized_filtered += 1
                continue

            raw_vertices = len(raw_poly.exterior.coords)
            simplified_poly = self.simplifier.simplify(raw_poly)
            simplified_vertices = len(simplified_poly.exterior.coords)

            parcel_id = f"VEC-PARCEL-{len(parcels) + 1:04d}"
            parcels.append(
                VectorizedParcel(
                    parcel_id=parcel_id,
                    polygon=simplified_poly,
                    area_sqm=float(simplified_poly.area),
                    perimeter_m=float(simplified_poly.length),
                    vertex_count_raw=raw_vertices,
                    vertex_count_simplified=simplified_vertices,
                    pixel_count=comp.pixel_count,
                )
            )

        total_area = sum(p.area_sqm for p in parcels)
        all_valid = all(p.polygon.is_valid and not p.polygon.is_empty for p in parcels)

        audit = VectorizationAudit(
            total_parcels_extracted=len(parcels),
            noise_components_filtered=noise_filtered,
            oversized_components_filtered=oversized_filtered,
            total_area_sqm=total_area,
            all_geometries_valid=all_valid,
        )

        return parcels, audit

    def vectorize_edge_mask(
        self, edge_mask: np.ndarray
    ) -> Tuple[List[VectorizedParcel], VectorizationAudit]:
        """Extract enclosed interior parcel polygons from a binary boundary edge ring mask."""
        inverted_mask = ~np.asarray(edge_mask, dtype=bool)
        return self.vectorize_foreground_mask(inverted_mask, exclude_border_components=True)
