"""Deterministic cadastral topology detection (overlaps, slivers, gaps, duplicate vertices, dangling nodes)."""

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

__all__ = [
    "PROJECTED_SRID",
    "OverlapCandidate",
    "build_pairwise_intersection_query",
    "compute_intersection_area_sqm",
    "detect_pairwise_overlaps",
    "extract_polygonal_intersection",
    "intersection_area_expr",
    "intersection_geometry_expr",
]
