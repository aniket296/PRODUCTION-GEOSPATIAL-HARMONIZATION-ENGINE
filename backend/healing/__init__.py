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
from backend.healing.sliver_union import (
    NeighborMetrics,
    SliverAssignment,
    SliverUnionResult,
    heal_layer_slivers,
    heal_slivers,
    sliver_union_geometry_expr,
)

__all__ = [
    "NeighborMetrics",
    "SharedBoundaryCandidateInput",
    "SharedBoundaryMatch",
    "SliverAssignment",
    "SliverUnionResult",
    "build_longest_shared_border_query",
    "compute_shared_boundary_length_m",
    "extract_shared_boundary_lines",
    "heal_layer_slivers",
    "heal_slivers",
    "identify_longest_shared_boundary_neighbor",
    "rank_shared_boundary_neighbors",
    "shared_boundary_geometry_expr",
    "shared_boundary_length_expr",
    "sliver_union_geometry_expr",
]
