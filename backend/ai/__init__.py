"""AI-driven boundary alignment and computer vision harmonization package."""

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

__all__ = [
    "AlignmentCategory",
    "AlignmentHint",
    "BoundaryAlignmentMetrics",
    "BoundaryAlignmentScorer",
    "compute_boundary_f1",
    "compute_centroid_displacement",
    "compute_hausdorff_distance",
    "compute_iou",
    "compute_modified_hausdorff_distance",
]
