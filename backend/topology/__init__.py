"""Deterministic cadastral topology detection (overlaps, slivers, gaps, duplicate vertices, dangling nodes)."""

from backend.topology.compactness import (
    FOUR_PI,
    CompactnessMetrics,
    compute_polsby_popper,
    evaluate_geometry_compactness,
    polsby_popper_expr,
)
from backend.topology.intersection import (
    PROJECTED_SRID,
    OverlapCandidate,
    build_pairwise_intersection_query,
    compute_intersection_area_sqm,
    detect_pairwise_overlaps,
    extract_polygonal_intersection,
    intersection_area_expr,
    intersection_geometry_expr,
)
from backend.topology.overlap_percentage import (
    OverlapPercentageMetrics,
    build_pairwise_overlap_percentage_query,
    compute_overlap_percentage,
    compute_overlap_percentages,
    evaluate_pair_overlap_metrics,
    overlap_percentage_expr,
)

__all__ = [
    "FOUR_PI",
    "PROJECTED_SRID",
    "CompactnessMetrics",
    "OverlapCandidate",
    "OverlapPercentageMetrics",
    "build_pairwise_intersection_query",
    "build_pairwise_overlap_percentage_query",
    "compute_intersection_area_sqm",
    "compute_overlap_percentage",
    "compute_overlap_percentages",
    "compute_polsby_popper",
    "detect_pairwise_overlaps",
    "evaluate_geometry_compactness",
    "evaluate_pair_overlap_metrics",
    "extract_polygonal_intersection",
    "intersection_area_expr",
    "intersection_geometry_expr",
    "overlap_percentage_expr",
    "polsby_popper_expr",
]
