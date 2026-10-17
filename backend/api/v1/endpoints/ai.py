"""AI-driven boundary alignment and satellite discrepancy scoring endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from fastapi import APIRouter, HTTPException, status
import numpy as np
from shapely.geometry import Polygon, mapping, shape

from backend.ai.alignment import BoundaryAlignmentScorer
from backend.ai.deformation import (
    DeformationAudit,
    DeformationConfig,
    DisplacementVector,
    ElasticDeformationEngine,
    build_displacement_field_from_pairs,
)
from backend.ai.super_resolution import (
    SuperResolutionConfig,
    SuperResolutionEngine,
)
from backend.ai.vectorization import (
    PolygonVectorizationEngine,
    VectorizationConfig,
)
from backend.api.schemas.ai import (
    AlignmentHintSchema,
    BatchBoundaryAlignmentRequest,
    BatchBoundaryAlignmentResponse,
    BoundaryAlignmentRequest,
    BoundaryAlignmentResponse,
    BoundaryMaskExtractRequest,
    BoundaryMaskExtractResponse,
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
    SuperResolutionAuditSchema,
    SuperResolutionConfigSchema,
    SuperResolutionEnhanceRequest,
    SuperResolutionEnhanceResponse,
    VectorizationAuditSchema,
    VectorizationConfigSchema,
    VectorizeMaskRequest,
    VectorizeMaskResponse,
    VectorizedParcelSchema,
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


def _build_sr_config(cfg_schema: SuperResolutionConfigSchema | None) -> SuperResolutionConfig:
    if cfg_schema is None:
        return SuperResolutionConfig()
    return SuperResolutionConfig(
        scale_factor=cfg_schema.scale_factor,
        sharpen_amount=cfg_schema.sharpen_amount,
        sharpen_radius=cfg_schema.sharpen_radius,
        apply_contrast_enhancement=cfg_schema.apply_contrast_enhancement,
        lower_percentile=cfg_schema.lower_percentile,
        upper_percentile=cfg_schema.upper_percentile,
        original_gsd_m=cfg_schema.original_gsd_m,
        edge_threshold=cfg_schema.edge_threshold,
    )


@router.post(
    "/super-resolution/enhance",
    response_model=SuperResolutionEnhanceResponse,
    status_code=status.HTTP_200_OK,
)
def enhance_raster_endpoint(request: SuperResolutionEnhanceRequest) -> SuperResolutionEnhanceResponse:
    """Super-resolve, contrast-stretch, and edge-sharpen a low-resolution satellite raster tile."""
    try:
        arr = np.asarray(request.raster_matrix, dtype=np.float32)
        if arr.ndim not in (2, 3) or arr.shape[0] < 2 or arr.shape[1] < 2:
            raise ValueError("Raster matrix must be 2D (H, W) or 3D (H, W, C) with H, W >= 2")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid raster_matrix: {e}",
        )

    engine = SuperResolutionEngine(config=_build_sr_config(request.config))
    enhanced, audit = engine.enhance(arr)

    return SuperResolutionEnhanceResponse(
        tile_id=request.tile_id,
        enhanced_matrix=np.round(enhanced, 4).tolist(),
        audit=SuperResolutionAuditSchema(
            original_shape=list(audit.original_shape),
            enhanced_shape=list(audit.enhanced_shape),
            scale_factor=audit.scale_factor,
            original_gsd_m=round(audit.original_gsd_m, 4),
            enhanced_gsd_m=round(audit.enhanced_gsd_m, 4),
            initial_sharpness=round(audit.initial_sharpness, 6),
            enhanced_sharpness=round(audit.enhanced_sharpness, 6),
            sharpness_gain_ratio=round(audit.sharpness_gain_ratio, 4),
            mean_radiance_before=round(audit.mean_radiance_before, 4),
            mean_radiance_after=round(audit.mean_radiance_after, 4),
        ),
    )


@router.post(
    "/super-resolution/boundary-mask",
    response_model=BoundaryMaskExtractResponse,
    status_code=status.HTTP_200_OK,
)
def extract_boundary_mask_endpoint(request: BoundaryMaskExtractRequest) -> BoundaryMaskExtractResponse:
    """Enhance a low-resolution satellite raster patch and extract binary parcel boundary mask."""
    try:
        arr = np.asarray(request.raster_matrix, dtype=np.float32)
        if arr.ndim not in (2, 3) or arr.shape[0] < 2 or arr.shape[1] < 2:
            raise ValueError("Raster matrix must be 2D (H, W) or 3D (H, W, C) with H, W >= 2")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid raster_matrix: {e}",
        )

    cfg = _build_sr_config(request.config)
    engine = SuperResolutionEngine(config=cfg)
    mask = engine.extract_boundary_mask(arr)

    total_pixels = int(mask.size)
    edge_count = int(np.count_nonzero(mask))
    density = (edge_count / total_pixels) if total_pixels > 0 else 0.0
    enhanced_gsd = cfg.original_gsd_m / float(cfg.scale_factor)

    return BoundaryMaskExtractResponse(
        tile_id=request.tile_id,
        boundary_mask=mask.tolist(),
        edge_pixel_count=edge_count,
        edge_density_ratio=round(density, 4),
        enhanced_gsd_m=round(enhanced_gsd, 4),
    )


@router.post(
    "/vectorization/vectorize",
    response_model=VectorizeMaskResponse,
    status_code=status.HTTP_200_OK,
)
def vectorize_mask_endpoint(request: VectorizeMaskRequest) -> VectorizeMaskResponse:
    """Vectorize a binary foreground or boundary edge mask into simplified GeoJSON parcel polygons."""
    try:
        mask = np.asarray(request.binary_mask, dtype=bool)
        if mask.ndim != 2 or mask.shape[0] < 2 or mask.shape[1] < 2:
            raise ValueError("binary_mask must be a 2D boolean matrix with dimensions >= 2x2")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid binary_mask: {e}",
        )

    cfg = VectorizationConfig()
    if request.config is not None:
        cfg = VectorizationConfig(
            origin_x=request.config.origin_x,
            origin_y=request.config.origin_y,
            gsd_m=request.config.gsd_m,
            simplify_tolerance_m=request.config.simplify_tolerance_m,
            min_area_sqm=request.config.min_area_sqm,
            max_area_sqm=request.config.max_area_sqm,
            preserve_topology=request.config.preserve_topology,
            connectivity=request.config.connectivity,
        )

    engine = PolygonVectorizationEngine(config=cfg)
    mask_type = request.mask_type.upper()
    if mask_type == "EDGE":
        parcels, audit = engine.vectorize_edge_mask(mask)
    else:
        parcels, audit = engine.vectorize_foreground_mask(mask)

    parcel_schemas = [
        VectorizedParcelSchema(
            parcel_id=p.parcel_id,
            geojson=mapping(p.polygon),
            area_sqm=round(p.area_sqm, 2),
            perimeter_m=round(p.perimeter_m, 2),
            vertex_count_raw=p.vertex_count_raw,
            vertex_count_simplified=p.vertex_count_simplified,
            pixel_count=p.pixel_count,
        )
        for p in parcels
    ]

    return VectorizeMaskResponse(
        tile_id=request.tile_id,
        parcels=parcel_schemas,
        audit=VectorizationAuditSchema(
            total_parcels_extracted=audit.total_parcels_extracted,
            noise_components_filtered=audit.noise_components_filtered,
            oversized_components_filtered=audit.oversized_components_filtered,
            total_area_sqm=round(audit.total_area_sqm, 2),
            all_geometries_valid=audit.all_geometries_valid,
        ),
    )



