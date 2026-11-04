"""Deterministic cadastral topology healing (sliver assignment, micro-overlap resolution, snapping)."""

from backend.healing.shared_boundary import (
    SharedBoundaryCandidateInput,
    SharedBoundaryMatch,
    build_longest_shared_border_query,
    compute_shared_boundary_length_m,
    extract_shared_boundary_lines,
    identify_longest_shared_boundary_neighbor,
    rank_shared_boundary_neighbors,
    shared_boundary_geometry_expr,
    shared_boundary_length_expr,
)

__all__ = [
    "SharedBoundaryCandidateInput",
    "SharedBoundaryMatch",
    "build_longest_shared_border_query",
    "compute_shared_boundary_length_m",
    "extract_shared_boundary_lines",
    "identify_longest_shared_boundary_neighbor",
    "rank_shared_boundary_neighbors",
    "shared_boundary_geometry_expr",
    "shared_boundary_length_expr",
]
