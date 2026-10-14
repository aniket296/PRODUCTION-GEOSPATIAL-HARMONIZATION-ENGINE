"""AI-driven boundary alignment and satellite discrepancy scoring endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from fastapi import APIRouter, HTTPException, status
from shapely.geometry import Polygon, mapping, shape

from backend.ai.alignment import BoundaryAlignmentScorer
from backend.ai.deformation import (
    DeformationAudit,
    DeformationConfig,
    DisplacementVector,
    ElasticDeformationEngine,
    build_displacement_field_from_pairs,
)
from backend.api.schemas.ai import (
    AlignmentHintSchema,
    BatchBoundaryAlignmentRequest,
    BatchBoundaryAlignmentResponse,
    BoundaryAlignmentRequest,
    BoundaryAlignmentResponse,
    ClusterParcelItem,
    DeformClusterRequest,
    DeformClusterResponse,
    DeformPolygonRequest,
    DeformPolygonResponse,
    DeformationAuditSchema,
    DeformationConfigSchema,
    DeformedParcelItem,
    DisplacementVectorSchema,
    SnapToSatelliteRequest,
    SnapToSatelliteResponse,
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


@router.post("/deformation/deform", response_model=DeformPolygonResponse, status_code=status.HTTP_200_OK)
def deform_polygon_endpoint(request: DeformPolygonRequest) -> DeformPolygonResponse:
    """Deform a parcel polygon geometry using custom displacement vectors."""
    try:
        geom = shape(request.geojson)
        if not isinstance(geom, Polygon):
            raise ValueError("Input geometry must be a Polygon")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid polygon geometry: {e}",
        )

    vectors = [
        DisplacementVector(
            x=v.x,
            y=v.y,
            dx=v.dx,
            dy=v.dy,
            weight=v.weight,
        )
        for v in request.displacement_vectors
    ]

    cfg = None
    kernel = "idw"
    if request.config:
        cfg = DeformationConfig(
            max_displacement_m=request.config.max_displacement_m,
            preserve_topology=request.config.preserve_topology,
            simplification_tolerance_m=request.config.simplify_tolerance,
        )
        kernel = request.config.interpolation_method.lower()

    engine = ElasticDeformationEngine(cfg)
    deformed_poly, audit = engine.deform_polygon(geom, vectors, kernel=kernel)

    return DeformPolygonResponse(
        parcel_id=request.parcel_id,
        deformed_geojson=mapping(deformed_poly),
        audit=DeformationAuditSchema(
            total_vertices_deformed=audit.total_vertices_deformed,
            clamped_vertices_count=audit.clamped_vertices_count,
            max_displacement_observed=round(audit.max_displacement_observed, 4),
            rms_displacement=round(audit.rms_displacement, 4),
            original_area_sqm=round(audit.original_area_sqm, 2),
            deformed_area_sqm=round(audit.deformed_area_sqm, 2),
            area_distortion_ratio=round(audit.area_distortion_ratio, 4),
            topology_preserved=audit.topology_preserved,
        ),
    )


@router.post("/deformation/snap", response_model=SnapToSatelliteResponse, status_code=status.HTTP_200_OK)
def snap_to_satellite_endpoint(request: SnapToSatelliteRequest) -> SnapToSatelliteResponse:
    """Snap a cadastral parcel to detected satellite imagery boundary."""
    try:
        cad_geom = shape(request.cadastre_geojson)
        if not isinstance(cad_geom, Polygon):
            raise ValueError("Cadastre geometry must be a Polygon")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid cadastre_geojson geometry: {e}",
        )

    try:
        sat_geom = shape(request.satellite_geojson)
        if not isinstance(sat_geom, Polygon):
            raise ValueError("Satellite geometry must be a Polygon")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid satellite_geojson geometry: {e}",
        )

    cfg = None
    if request.config:
        cfg = DeformationConfig(
            max_displacement_m=request.config.max_displacement_m,
            preserve_topology=request.config.preserve_topology,
            simplification_tolerance_m=request.config.simplify_tolerance,
        )

    engine = ElasticDeformationEngine(cfg)
    perimeter = cad_geom.exterior.length
    sample_spacing_m = max(1.0, perimeter / request.num_boundary_samples)

    vectors = build_displacement_field_from_pairs(cad_geom, sat_geom, sample_spacing_m=sample_spacing_m)
    deformed_poly, audit = engine.deform_polygon(cad_geom, vectors)

    return SnapToSatelliteResponse(
        parcel_id=request.parcel_id,
        deformed_geojson=mapping(deformed_poly),
        audit=DeformationAuditSchema(
            total_vertices_deformed=audit.total_vertices_deformed,
            clamped_vertices_count=audit.clamped_vertices_count,
            max_displacement_observed=round(audit.max_displacement_observed, 4),
            rms_displacement=round(audit.rms_displacement, 4),
            original_area_sqm=round(audit.original_area_sqm, 2),
            deformed_area_sqm=round(audit.deformed_area_sqm, 2),
            area_distortion_ratio=round(audit.area_distortion_ratio, 4),
            topology_preserved=audit.topology_preserved,
        ),
        generated_vectors_count=len(vectors),
    )


@router.post("/deformation/cluster", response_model=DeformClusterResponse, status_code=status.HTTP_200_OK)
def deform_cluster_endpoint(request: DeformClusterRequest) -> DeformClusterResponse:
    """Deform a cluster of contiguous cadastral parcels under a cohesive displacement field."""
    parcels_map: List[Tuple[str, Polygon]] = []
    for p in request.parcels:
        try:
            poly = shape(p.geojson)
            if not isinstance(poly, Polygon):
                raise ValueError("Each parcel must be a Polygon")
            parcels_map.append((p.parcel_id, poly))
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid geometry for parcel {p.parcel_id}: {e}",
            )

    vectors = [
        DisplacementVector(
            x=v.x,
            y=v.y,
            dx=v.dx,
            dy=v.dy,
            weight=v.weight,
        )
        for v in request.displacement_vectors
    ]

    cfg = None
    kernel = "idw"
    if request.config:
        cfg = DeformationConfig(
            max_displacement_m=request.config.max_displacement_m,
            preserve_topology=request.config.preserve_topology,
            simplification_tolerance_m=request.config.simplify_tolerance,
        )
        kernel = request.config.interpolation_method.lower()

    engine = ElasticDeformationEngine(cfg)
    results: List[DeformedParcelItem] = []

    for parcel_id, poly in parcels_map:
        deformed_poly, audit = engine.deform_polygon(poly, vectors, kernel=kernel)
        results.append(
            DeformedParcelItem(
                parcel_id=parcel_id,
                deformed_geojson=mapping(deformed_poly),
                audit=DeformationAuditSchema(
                    total_vertices_deformed=audit.total_vertices_deformed,
                    clamped_vertices_count=audit.clamped_vertices_count,
                    max_displacement_observed=round(audit.max_displacement_observed, 4),
                    rms_displacement=round(audit.rms_displacement, 4),
                    original_area_sqm=round(audit.original_area_sqm, 2),
                    deformed_area_sqm=round(audit.deformed_area_sqm, 2),
                    area_distortion_ratio=round(audit.area_distortion_ratio, 4),
                    topology_preserved=audit.topology_preserved,
                ),
            )
        )

    return DeformClusterResponse(
        total_parcels=len(results),
        results=results,
    )

