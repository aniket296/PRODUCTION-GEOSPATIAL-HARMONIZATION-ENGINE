"""Geodetic and Coordinate Reference System (CRS) management API endpoints."""

from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, status

from backend.api.schemas.geodetic import (
    CRSDetailResponse,
    CRSSuggestionRequest,
    CRSSuggestionResponse,
    CRSSummaryResponse,
    CRSValidationRequest,
    CRSValidationResponse,
    CoordinateTransformBatchRequest,
    CoordinateTransformBatchResponse,
    CoordinateTransformPointRequest,
    CoordinateTransformPointResponse,
    HelmertFitRequest,
    HelmertFitResponse,
    LocalCadastralGridRequest,
    ToposheetResponse,
)
from backend.geodetic import (
    CRSNotFoundError,
    CRSOutOfBoundsError,
    CRSValidationError,
    HelmertConvention,
    HelmertFitter,
    LocalCadastralGrid,
    TransformationError,
    crs_registry,
    get_transformation_pipeline,
    transform_coordinates_batch,
)

router = APIRouter(prefix="/geodetic", tags=["geodetic"])


@router.get("/crs", response_model=List[CRSSummaryResponse], status_code=status.HTTP_200_OK)
def list_or_search_crs(
    q: Optional[str] = Query(None, description="Search term for CRS name, code, or alias"),
    proj_type: Optional[str] = Query(None, description="Filter by projection type (e.g. PROJECTED, GEOGRAPHIC_2D, LOCAL)"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of records to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> List[CRSSummaryResponse]:
    """Retrieve or search registered coordinate reference systems."""
    if q:
        records = crs_registry.search(q, limit=limit + offset)
    else:
        records = crs_registry.list_supported()

    if proj_type:
        records = [r for r in records if r.proj_type.upper() == proj_type.upper()]

    paginated = records[offset : offset + limit]
    return [
        CRSSummaryResponse(
            srid=r.srid,
            auth_name=r.auth_name,
            auth_code=str(r.auth_code),
            epsg_code=r.epsg_code,
            name=r.name,
            proj_type=r.proj_type,
            unit=r.unit,
            area_of_use_name=r.area_of_use_name,
            area_sq_deg=r.area_sq_deg,
        )
        for r in paginated
    ]


@router.get("/crs/{identifier}", response_model=CRSDetailResponse, status_code=status.HTTP_200_OK)
def get_crs_detail(identifier: str) -> CRSDetailResponse:
    """Retrieve full geodetic parameters, ellipsoid geometry, and bounds for a given CRS."""
    record = crs_registry.get(identifier, strict=False)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Coordinate Reference System '{identifier}' not found in registry.",
        )

    return CRSDetailResponse(
        srid=record.srid,
        auth_name=record.auth_name,
        auth_code=str(record.auth_code),
        epsg_code=record.epsg_code,
        name=record.name,
        proj_type=record.proj_type,
        datum_name=record.datum_name,
        ellipsoid={
            "name": record.ellipsoid.name,
            "a": record.ellipsoid.semi_major_axis,
            "b": record.ellipsoid.semi_minor_axis,
            "rf": record.ellipsoid.inverse_flattening,
        },
        unit=record.unit,
        area_of_use_name=record.area_of_use_name,
        bounds=list(record.bounds),
        area_sq_deg=record.area_sq_deg,
        aliases=record.aliases,
        wkt2=record.wkt2,
        proj4=record.proj4,
    )


@router.post("/crs/validate", response_model=CRSValidationResponse, status_code=status.HTTP_200_OK)
def validate_crs_bounds(request: CRSValidationRequest) -> CRSValidationResponse:
    """Validate whether input coordinates or bounding box fall within the CRS geographic domain."""
    if len(request.coordinates) not in (2, 4):
        return CRSValidationResponse(
            is_valid_syntax=False,
            in_bounds=False,
            identifier=request.identifier,
            error_detail="Coordinates must be a 2-tuple [lon, lat] or 4-tuple [min_lon, min_lat, max_lon, max_lat].",
        )

    try:
        record = crs_registry.get(request.identifier, strict=True)
    except CRSNotFoundError:
        return CRSValidationResponse(
            is_valid_syntax=False,
            in_bounds=False,
            identifier=request.identifier,
            error_detail=f"Unrecognized CRS identifier '{request.identifier}'.",
        )

    coords_tuple = tuple(request.coordinates)  # type: ignore
    try:
        crs_registry.validate_bounds(request.identifier, coords_tuple)
        return CRSValidationResponse(
            is_valid_syntax=True,
            in_bounds=True,
            identifier=request.identifier,
            resolved_srid=record.srid,
        )
    except CRSOutOfBoundsError as e:
        return CRSValidationResponse(
            is_valid_syntax=True,
            in_bounds=False,
            identifier=request.identifier,
            resolved_srid=record.srid,
            error_detail=str(e),
        )


@router.post("/crs/suggest", response_model=CRSSuggestionResponse, status_code=status.HTTP_200_OK)
def suggest_crs(request: CRSSuggestionRequest) -> CRSSuggestionResponse:
    """Recommend optimal WGS 84 UTM zone and historical Kalianpur 1975 zone for coordinates."""
    lon, lat = request.longitude, request.latitude

    matched_state_name: Optional[str] = None
    if request.state:
        state_info = crs_registry.get_crs_for_state(request.state)
        if state_info:
            matched_state_name = state_info["state_name"]

    if request.toposheet:
        try:
            sheet_data = crs_registry.parse_soi_toposheet(request.toposheet)
            lon, lat = sheet_data["centroid"]
        except CRSValidationError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    try:
        utm_epsg = crs_registry.suggest_utm_zone(lon, lat)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    soi_epsg = crs_registry.suggest_india_zone(lon, lat)

    return CRSSuggestionResponse(
        centroid=[lon, lat],
        suggested_utm_epsg=utm_epsg,
        suggested_utm_srid=f"EPSG:{utm_epsg}",
        suggested_soi_epsg=soi_epsg,
        suggested_soi_srid=f"EPSG:{soi_epsg}" if soi_epsg else None,
        state_name=matched_state_name,
    )


@router.post("/crs/local-grid", response_model=CRSDetailResponse, status_code=status.HTTP_201_CREATED)
def register_local_grid(request: LocalCadastralGridRequest) -> CRSDetailResponse:
    """Register a custom local village cadastral survey engineering reference frame."""
    local_grid = LocalCadastralGrid(
        grid_id=request.grid_id,
        name=request.name,
        village_name=request.village_name,
        district=request.district,
        state=request.state,
        origin_lon=request.origin_lon,
        origin_lat=request.origin_lat,
        false_easting=request.false_easting,
        false_northing=request.false_northing,
        scale_factor=request.scale_factor,
        azimuth_rotation_deg=request.azimuth_rotation_deg,
        extent_radius_km=request.extent_radius_km,
    )

    record = crs_registry.register_local_cadastral_grid(local_grid)

    return CRSDetailResponse(
        srid=record.srid,
        auth_name=record.auth_name,
        auth_code=str(record.auth_code),
        epsg_code=record.epsg_code,
        name=record.name,
        proj_type=record.proj_type,
        datum_name=record.datum_name,
        ellipsoid={
            "name": record.ellipsoid.name,
            "a": record.ellipsoid.semi_major_axis,
            "b": record.ellipsoid.semi_minor_axis,
            "rf": record.ellipsoid.inverse_flattening,
        },
        unit=record.unit,
        area_of_use_name=record.area_of_use_name,
        bounds=list(record.bounds),
        area_sq_deg=record.area_sq_deg,
        aliases=record.aliases,
        wkt2=record.wkt2,
        proj4=record.proj4,
    )


@router.get("/toposheet/{sheet_id}", response_model=ToposheetResponse, status_code=status.HTTP_200_OK)
def parse_toposheet(sheet_id: str) -> ToposheetResponse:
    """Parse Survey of India Toposheet sheet ID to geographic bounds and suggested CRS."""
    try:
        data = crs_registry.parse_soi_toposheet(sheet_id)
        return ToposheetResponse(
            sheet_id=data["sheet_id"],
            scale=data["scale"],
            bounds=list(data["bounds"]),
            centroid=list(data["centroid"]),
            suggested_utm_epsg=data["suggested_utm_epsg"],
            suggested_soi_epsg=data["suggested_soi_epsg"],
        )
    except CRSValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/transform/point", response_model=CoordinateTransformPointResponse, status_code=status.HTTP_200_OK)
def transform_point_coordinate(request: CoordinateTransformPointRequest) -> CoordinateTransformPointResponse:
    """Transform a single coordinate between two geodetic datums."""
    pipeline = get_transformation_pipeline(request.source_srid, request.target_srid)
    if not pipeline:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to establish transformation pipeline from '{request.source_srid}' to '{request.target_srid}'.",
        )

    coords = request.coordinates
    if len(coords) < 2 or len(coords) > 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Coordinates must have 2 elements [lon, lat] or 3 elements [lon, lat, height].",
        )

    lon, lat = coords[0], coords[1]
    h = coords[2] if len(coords) == 3 else 0.0

    try:
        t_lon, t_lat, t_h = pipeline.transform(lon, lat, h)
    except TransformationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    out_coords = [t_lon, t_lat, t_h] if len(coords) == 3 else [t_lon, t_lat]

    return CoordinateTransformPointResponse(
        source_srid=request.source_srid,
        target_srid=request.target_srid,
        transformed_coordinates=out_coords,
    )


@router.post("/transform/batch", response_model=CoordinateTransformBatchResponse, status_code=status.HTTP_200_OK)
def transform_batch_coordinates(request: CoordinateTransformBatchRequest) -> CoordinateTransformBatchResponse:
    """Batch transform multiple coordinates between two geodetic datums."""
    pipeline = get_transformation_pipeline(request.source_srid, request.target_srid)
    if not pipeline:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to establish transformation pipeline from '{request.source_srid}' to '{request.target_srid}'.",
        )

    tuple_coords = [tuple(c) for c in request.coordinates]
    try:
        transformed = transform_coordinates_batch(tuple_coords, pipeline)
    except TransformationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return CoordinateTransformBatchResponse(
        source_srid=request.source_srid,
        target_srid=request.target_srid,
        transformed_coordinates=[list(c) for c in transformed],
        count=len(transformed),
    )


@router.post("/transform/fit-helmert", response_model=HelmertFitResponse, status_code=status.HTTP_200_OK)
def fit_helmert_from_gcps(request: HelmertFitRequest) -> HelmertFitResponse:
    """Compute least-squares 7-parameter Helmert transformation from Ground Control Point pairs."""
    try:
        convention_enum = HelmertConvention(request.convention.upper())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid convention '{request.convention}'. Must be 'POSITION_VECTOR' or 'COORDINATE_FRAME'.",
        )

    source_pts = [tuple(g.source_xyz) for g in request.gcps]  # type: ignore
    target_pts = [tuple(g.target_xyz) for g in request.gcps]  # type: ignore

    try:
        result = HelmertFitter.fit(
            source_pts,
            target_pts,
            convention=convention_enum,
            is_molodensky_badekas=request.is_molodensky_badekas,
        )
    except TransformationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    res_dict = result.to_dict()
    return HelmertFitResponse(
        parameters=res_dict["parameters"],
        rmse_3d=res_dict["rmse_3d"],
        rmse_x=res_dict["rmse_x"],
        rmse_y=res_dict["rmse_y"],
        rmse_z=res_dict["rmse_z"],
        num_points=res_dict["num_points"],
        degrees_of_freedom=res_dict["degrees_of_freedom"],
        sigma_0=res_dict["sigma_0"],
        residuals=res_dict["residuals"],
    )

