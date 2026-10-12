"""AI-driven boundary alignment and satellite discrepancy scoring endpoints."""

from __future__ import annotations

from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException, status
from shapely.geometry import shape

from backend.ai.alignment import BoundaryAlignmentScorer
from backend.api.schemas.ai import (
    AlignmentHintSchema,
    BatchBoundaryAlignmentRequest,
    BatchBoundaryAlignmentResponse,
    BoundaryAlignmentRequest,
    BoundaryAlignmentResponse,
)

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/alignment/score", response_model=BoundaryAlignmentResponse, status_code=status.HTTP_200_OK)
def score_boundary_alignment(request: BoundaryAlignmentRequest) -> BoundaryAlignmentResponse:
    """Evaluate geometric alignment, IoU, Hausdorff distance, and boundary F1 between cadastre and satellite geometries."""
    try:
        cad_geom = shape(request.cadastre_geojson)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid cadastre_geojson geometry: {e}",
        )

    try:
        sat_geom = shape(request.satellite_geojson)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid satellite_geojson geometry: {e}",
        )

    scorer = BoundaryAlignmentScorer(tolerance_buffer_m=request.tolerance_buffer_m)
    metrics = scorer.evaluate(cad_geom, sat_geom, parcel_id=request.parcel_id)

    hint_schema = None
    if metrics.alignment_hint:
        h = metrics.alignment_hint
        hint_schema = AlignmentHintSchema(
            suggested_dx=h.suggested_dx,
            suggested_dy=h.suggested_dy,
            confidence=h.confidence,
            notes=h.notes,
        )

    return BoundaryAlignmentResponse(
        parcel_id=metrics.parcel_id,
        iou=metrics.iou,
        hausdorff_distance=metrics.hausdorff_distance,
        modified_hausdorff_distance=metrics.modified_hausdorff_distance,
        boundary_precision=metrics.boundary_precision,
        boundary_recall=metrics.boundary_recall,
        boundary_f1=metrics.boundary_f1,
        centroid_displacement_x=metrics.centroid_displacement_x,
        centroid_displacement_y=metrics.centroid_displacement_y,
        centroid_displacement_distance=metrics.centroid_displacement_distance,
        area_cadastre_sqm=metrics.area_cadastre_sqm,
        area_satellite_sqm=metrics.area_satellite_sqm,
        area_ratio=metrics.area_ratio,
        perimeter_cadastre_m=metrics.perimeter_cadastre_m,
        perimeter_satellite_m=metrics.perimeter_satellite_m,
        overall_score=metrics.overall_score,
        category=metrics.category.value,
        alignment_hint=hint_schema,
    )


@router.post("/alignment/batch", response_model=BatchBoundaryAlignmentResponse, status_code=status.HTTP_200_OK)
def score_boundary_alignment_batch(request: BatchBoundaryAlignmentRequest) -> BatchBoundaryAlignmentResponse:
    """Evaluate multiple cadastre-satellite parcel boundary alignments in batch."""
    results: List[BoundaryAlignmentResponse] = []
    excellent = 0
    acceptable = 0
    poor = 0
    misaligned = 0
    total_iou = 0.0
    total_score = 0.0

    for item in request.items:
        res = score_boundary_alignment(item)
        results.append(res)
        total_iou += res.iou
        total_score += res.overall_score

        if res.category == "EXCELLENT":
            excellent += 1
        elif res.category == "ACCEPTABLE":
            acceptable += 1
        elif res.category == "POOR":
            poor += 1
        else:
            misaligned += 1

    total = len(results)
    avg_iou = (total_iou / total) if total > 0 else 0.0
    avg_score = (total_score / total) if total > 0 else 0.0

    return BatchBoundaryAlignmentResponse(
        total_evaluated=total,
        excellent_count=excellent,
        acceptable_count=acceptable,
        poor_count=poor,
        misaligned_count=misaligned,
        average_iou=round(avg_iou, 4),
        average_score=round(avg_score, 2),
        results=results,
    )
