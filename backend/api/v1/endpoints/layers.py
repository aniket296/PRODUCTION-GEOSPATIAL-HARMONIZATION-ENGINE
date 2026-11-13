"""API endpoints for cadastral survey layer ingestion, upload, and status tracking."""

from __future__ import annotations

import logging
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from geoalchemy2.shape import to_shape
from shapely.geometry import box as shapely_box
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas.layer import (
    LayerExportPresignRequest,
    LayerExportPresignResponse,
    LayerStatusResponse,
    LayerUploadResponse,
)
from backend.api.schemas.topology import (
    AutoResolveRequest,
    AutoResolveResponse,
    TopologyErrorFeatureCollection,
    build_topology_error_feature_collection,
)
from backend.core.config import get_settings
from backend.db.session import get_async_session
from backend.export.flatgeobuf import (
    export_parcels_to_flatgeobuf,
    stream_flatgeobuf_bytes,
)
from backend.export.geojson import export_parcels_to_geojson_bytes
from backend.export.geopackage import export_parcels_to_geopackage
from backend.export.manifest import build_harmonization_audit_manifest
from backend.export.staging import SUPPORTED_EXPORT_FORMATS, stage_harmonized_export
from backend.models.topology import TopologicalError
from backend.storage.minio_client import MinioStorageClient, get_storage_client
from backend.healing.orchestrator import (
    HealingTransactionRollbackError,
    capture_parcel_snapshots,
    execute_healing_transaction,
    restore_parcel_snapshots,
)
from backend.healing.verification import (
    DEFAULT_RESOLVED_BY,
    PostResolutionVerificationError,
    verify_layer_post_resolution,
)
from backend.ingestion.validation import UploadValidator
from backend.models.layer import LayerStatus, RawCadastralLayer
from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.worker.celery_app import process_cadastral_layer_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/layers", tags=["Cadastral Layers"])


@router.post(
    "/upload",
    response_model=LayerUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload cadastral survey spatial layer for asynchronous ingestion",
)
async def upload_cadastral_layer(
    response: Response,
    file: UploadFile = File(..., description="Geospatial survey dataset (GeoJSON, Shapefile ZIP, FlatGeobuf, DXF, GeoTIFF)"),
    layer_name: Optional[str] = Form(None, description="Optional human-readable cadastral layer name"),
    declared_crs: Optional[str] = Form(None, description="Declared CRS (e.g., EPSG:32643)"),
    target_crs: Optional[str] = Form("EPSG:4326", description="Target harmonization CRS"),
    normalize_geometries: bool = Form(True, description="Whether to automatically cast to 2D MultiPolygons"),
    session: Optional[AsyncSession] = Depends(get_async_session),
) -> LayerUploadResponse:
    """Accept and schedule cadastral vector or raster layers for distributed processing.

    Returns HTTP 202 with assigned `job_id` and `layer_id`.
    """
    settings = get_settings()

    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must have a valid filename",
        )

    # 1. Read payload
    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read uploaded file payload: {exc}",
        )

    file_size = len(content)
    if file_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded payload contains 0 bytes",
        )

    if file_size > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Uploaded file exceeds maximum limit of {settings.max_upload_size_bytes} bytes",
        )

    # 2. Pre-flight security & format verification
    validation_report = UploadValidator.validate(
        data=content,
        filename=file.filename,
        declared_mime_type=file.content_type,
    )
    if not validation_report.is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload rejected: {validation_report.error_message} ({validation_report.error_code})",
        )

    # 3. Store file to staging directory
    layer_id = uuid.uuid4()
    upload_dir = Path(tempfile.gettempdir()) / "cadastre_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    clean_filename = f"{layer_id}_{Path(file.filename).name}"
    staged_path = upload_dir / clean_filename
    staged_path.write_bytes(content)

    resolved_name = layer_name or Path(file.filename).stem
    detected_format = validation_report.format.value if validation_report.format else "UNKNOWN"
    storage_path = str(staged_path)

    # Attempt upload to S3/MinIO bucket (best effort, graceful fallback to staging disk)
    try:
        from backend.storage.minio_client import MinioStorageClient

        storage_client = MinioStorageClient()
        object_name = f"{layer_id}/{file.filename}"
        storage_client.upload_file(
            bucket_name=settings.minio_bucket_raw,
            object_name=object_name,
            file_data=content,
            content_type=validation_report.mime_type or file.content_type,
            metadata={
                "layer_id": str(layer_id),
                "format": detected_format,
                "original_filename": file.filename,
            },
        )
        storage_path = f"s3://{settings.minio_bucket_raw}/{object_name}"
    except Exception as exc:
        logger.debug("MinIO S3 upload skipped or unreachable: %s", exc)

    now = datetime.now(timezone.utc)
    metadata_payload = {
        "upload_filename": file.filename,
        "declared_mime_type": file.content_type,
        "detected_format": detected_format,
        "file_size_bytes": file_size,
        "normalize_geometries": normalize_geometries,
        "staging_path": str(staged_path),
        "storage_path": storage_path,
        "validation_details": validation_report.details,
    }

    # 4. Dispatch Celery task
    try:
        celery_task = process_cadastral_layer_task.delay(
            layer_id=str(layer_id),
            file_path=str(staged_path),
            target_crs=target_crs,
            normalize_geometries=normalize_geometries,
        )
        job_id = celery_task.id
    except Exception as err:
        logger.warning("Celery dispatch unavailable or falling back: %s", err)
        job_id = f"job-{uuid.uuid4()}"

    metadata_payload["job_id"] = job_id

    # 5. Best-effort DB persistence (handles both live database and test environments)
    if session is not None:
        try:
            raw_layer = RawCadastralLayer(
                name=resolved_name,
                filename=file.filename,
                file_format=detected_format,
                storage_path=storage_path,
                source_crs=declared_crs,
                target_crs=target_crs or "EPSG:4326",
                status=LayerStatus.PENDING.value,
                metadata_json=metadata_payload,
            )
            raw_layer.id = layer_id
            session.add(raw_layer)
            await session.commit()
        except Exception as err:
            logger.warning("Could not persist layer record to DB in upload handler: %s", err)
            await session.rollback()

    # Set Location header pointing to layer status polling endpoint
    response.headers["Location"] = f"/api/v1/layers/{layer_id}/status"

    return LayerUploadResponse(
        layer_id=str(layer_id),
        job_id=job_id,
        status=LayerStatus.PENDING.value,
        message="Layer upload accepted for asynchronous processing",
        filename=file.filename,
        file_size_bytes=file_size,
        detected_format=detected_format,
        declared_crs=declared_crs,
        target_crs=target_crs or "EPSG:4326",
        created_at=now,
    )


@router.get(
    "/{layer_id}/status",
    response_model=LayerStatusResponse,
    summary="Get processing lifecycle status and progress of a cadastral layer",
)
async def get_layer_status(
    layer_id: uuid.UUID,
    session: Optional[AsyncSession] = Depends(get_async_session),
) -> LayerStatusResponse:
    """Retrieve real-time processing status, progress percentage, feature count, and diagnostics for a cadastral layer."""
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable",
        )

    stmt = select(RawCadastralLayer).where(RawCadastralLayer.id == layer_id)
    result = await session.execute(stmt)
    layer = result.scalar_one_or_none()

    if layer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cadastral layer {layer_id} not found",
        )

    # Progress and stage resolution
    status_val = layer.status
    stage: str
    progress_percent: int

    if status_val == LayerStatus.COMPLETED.value:
        stage = "COMPLETED"
        progress_percent = 100
    elif status_val == LayerStatus.FAILED.value:
        stage = "FAILED"
        progress_percent = 100
    elif status_val == LayerStatus.PROCESSING.value:
        stage = "INGESTING_FEATURES"
        progress_percent = 50
    elif status_val == LayerStatus.CALIBRATED.value:
        stage = "CALIBRATED"
        progress_percent = 100
    else:  # PENDING or UPLOADING
        stage = "QUEUED"
        progress_percent = 0

    # Live Celery AsyncResult inspection if background task is active
    job_id = layer.metadata_json.get("job_id") if layer.metadata_json else None
    if job_id and status_val in (LayerStatus.PENDING.value, LayerStatus.PROCESSING.value):
        try:
            from backend.worker.celery_app import celery_app

            async_res = celery_app.AsyncResult(job_id)
            if async_res.state == "PROGRESS":
                info = async_res.info or {}
                if isinstance(info, dict):
                    stage = info.get("stage", stage)
                    progress_percent = int(info.get("percent", progress_percent))
            elif async_res.state == "SUCCESS":
                stage = "COMPLETED"
                progress_percent = 100
            elif async_res.state == "FAILURE":
                stage = "FAILED"
                progress_percent = 100
                if not layer.error_message and async_res.result:
                    layer.error_message = str(async_res.result)
        except Exception as exc:
            logger.debug("Could not inspect Celery AsyncResult for %s: %s", job_id, exc)

    # Bounds resolution
    resolved_bounds: Optional[List[float]] = None
    if layer.bounding_box:
        if isinstance(layer.bounding_box, dict):
            if all(k in layer.bounding_box for k in ("minx", "miny", "maxx", "maxy")):
                resolved_bounds = [
                    float(layer.bounding_box["minx"]),
                    float(layer.bounding_box["miny"]),
                    float(layer.bounding_box["maxx"]),
                    float(layer.bounding_box["maxy"]),
                ]
            elif "bounds" in layer.bounding_box and isinstance(layer.bounding_box["bounds"], list):
                resolved_bounds = [float(x) for x in layer.bounding_box["bounds"]]
        elif isinstance(layer.bounding_box, (list, tuple)):
            resolved_bounds = [float(x) for x in layer.bounding_box]

    is_raster = bool(
        layer.file_format in ("GEOTIFF", "TIFF")
        or (layer.metadata_json and layer.metadata_json.get("is_raster", False))
    )

    return LayerStatusResponse(
        layer_id=str(layer.id),
        name=layer.name,
        filename=layer.filename,
        file_format=layer.file_format,
        status=layer.status,
        stage=stage,
        progress_percent=progress_percent,
        feature_count=layer.feature_count or 0,
        total_area_sqm=layer.total_area_sqm,
        is_raster=is_raster,
        detected_crs=layer.source_crs,
        bounds=resolved_bounds,
        error_message=layer.error_message,
        job_id=job_id,
        created_at=layer.created_at,
        updated_at=layer.updated_at,
        metadata=layer.metadata_json,
    )


@router.get(
    "/{layer_id}/topology-errors",
    response_model=TopologyErrorFeatureCollection,
    summary="Get detected topological errors for a cadastral layer as an RFC 7946 GeoJSON FeatureCollection",
)
async def get_layer_topology_errors(
    layer_id: uuid.UUID,
    severity: Optional[TopologySeverity] = None,
    error_type: Optional[TopologyErrorType] = None,
    is_resolved: Optional[bool] = None,
    session: Optional[AsyncSession] = Depends(get_async_session),
) -> TopologyErrorFeatureCollection:
    """Return all or filtered topological conflict records for `layer_id` as an RFC 7946 GeoJSON FeatureCollection."""
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable",
        )

    layer_stmt = select(RawCadastralLayer).where(RawCadastralLayer.id == layer_id)
    layer_res = await session.execute(layer_stmt)
    layer = layer_res.scalar_one_or_none()
    if layer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cadastral layer {layer_id} not found",
        )

    err_stmt = select(TopologicalError).where(TopologicalError.layer_id == layer_id)
    if severity is not None:
        err_stmt = err_stmt.where(TopologicalError.severity == severity.value)
    if error_type is not None:
        err_stmt = err_stmt.where(TopologicalError.error_type == error_type.value)
    if is_resolved is not None:
        err_stmt = err_stmt.where(TopologicalError.is_resolved.is_(is_resolved))
    err_stmt = err_stmt.order_by(TopologicalError.created_at, TopologicalError.id)

    err_res = await session.execute(err_stmt)
    errors = list(err_res.scalars().all())

    return build_topology_error_feature_collection(layer_id=layer_id, errors=errors)


@router.post(
    "/{layer_id}/auto-resolve",
    response_model=AutoResolveResponse,
    summary="Atomically execute deterministic topological healing on a cadastral layer",
)
async def auto_resolve_layer_topology(
    layer_id: uuid.UUID,
    payload: Optional[AutoResolveRequest] = Body(default=None),
    session: Optional[AsyncSession] = Depends(get_async_session),
) -> AutoResolveResponse:
    """Run deterministic topological healing, verify post-resolution cleanliness, and commit audit updates."""
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable",
        )

    layer_stmt = select(RawCadastralLayer).where(RawCadastralLayer.id == layer_id)
    layer_res = await session.execute(layer_stmt)
    layer = layer_res.scalar_one_or_none()
    if layer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cadastral layer {layer_id} not found",
        )

    parcel_stmt = (
        select(HarmonizedParcel)
        .where(HarmonizedParcel.layer_id == layer_id)
        .order_by(HarmonizedParcel.id)
    )
    parcel_res = await session.execute(parcel_stmt)
    parcels = list(parcel_res.scalars().all())

    err_stmt = (
        select(TopologicalError)
        .where(TopologicalError.layer_id == layer_id)
        .order_by(TopologicalError.created_at, TopologicalError.id)
    )
    err_res = await session.execute(err_stmt)
    errors = list(err_res.scalars().all())

    resolved_by = payload.resolved_by if payload is not None else DEFAULT_RESOLVED_BY
    strict_verification = payload.strict_verification if payload is not None else True

    snapshots = capture_parcel_snapshots(parcels)

    try:
        pipeline_result = await execute_healing_transaction(session, parcels)
        verification = await verify_layer_post_resolution(
            session,
            parcels,
            errors,
            pipeline_result=pipeline_result,
            resolved_by=resolved_by,
            strict=strict_verification,
        )
    except HealingTransactionRollbackError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except PostResolutionVerificationError as exc:
        restore_parcel_snapshots(parcels, snapshots)
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    await session.commit()

    remaining_disputes_count = len(verification.remaining_dispute_error_ids) or (
        1 if verification.residual_critical_dispute_count > 0 else 0
    )
    outcome_status = (
        "COMPLETED_WITH_DISPUTES"
        if remaining_disputes_count > 0 or len(pipeline_result.disputed_parcel_ids) > 0
        else "COMPLETED"
    )

    return AutoResolveResponse(
        layer_id=str(layer_id),
        status=outcome_status,
        total_parcels=len(parcels),
        healed_parcel_count=len(pipeline_result.auto_resolved_parcel_ids),
        slivers_merged_count=len(pipeline_result.sliver_result.assignments),
        micro_overlaps_resolved_count=len(pipeline_result.micro_overlap_result.resolved_pairs),
        boundaries_snapped_count=len(pipeline_result.snap_result.updated_geometries),
        grid_normalized_count=len(pipeline_result.grid_result.updated_geometries),
        resolved_errors_count=len(verification.resolved_error_ids),
        remaining_disputes_count=len(verification.remaining_dispute_error_ids),
        residual_auto_resolvable_count=verification.residual_auto_resolvable_count,
        dispute_locked_parcel_ids=[str(pid) for pid in pipeline_result.disputed_parcel_ids],
        auto_resolved_parcel_ids=[str(pid) for pid in pipeline_result.auto_resolved_parcel_ids],
        resolved_error_ids=[str(eid) for eid in verification.resolved_error_ids],
        remaining_dispute_error_ids=[str(eid) for eid in verification.remaining_dispute_error_ids],
        verified_at=verification.verified_at,
    )


def _parse_wgs84_bbox(bbox: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    """Parse and validate a comma-separated WGS 84 bounding box `min_lon,min_lat,max_lon,max_lat`."""
    if bbox is None:
        return None
    parts = [part.strip() for part in bbox.split(",")]
    if len(parts) != 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bbox must contain 4 comma-separated coordinates: min_lon,min_lat,max_lon,max_lat",
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bbox coordinates must be numeric floats",
        ) from exc

    if not all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bbox coordinates must be finite numbers",
        )
    if min_lon > max_lon or min_lat > max_lat:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bbox min coordinates must be <= max coordinates",
        )
    return (min_lon, min_lat, max_lon, max_lat)


def _parse_byte_range(range_header: str, total_bytes: int) -> Optional[tuple[int, int]]:
    """Parse a single HTTP `Range: bytes=...` header against `total_bytes`.

    Returns `(start, end)` inclusive byte indices, or `None` when unsatisfiable/malformed.
    """
    if total_bytes <= 0 or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes=") :].strip()
    if "," in spec or "-" not in spec:
        return None

    start_str, end_str = spec.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()

    try:
        if start_str == "":
            # Suffix range: bytes=-N
            suffix_len = int(end_str)
            if suffix_len <= 0:
                return None
            start = max(0, total_bytes - suffix_len)
            end = total_bytes - 1
            return (start, end)

        start = int(start_str)
        if start < 0 or start >= total_bytes:
            return None

        if end_str == "":
            end = total_bytes - 1
        else:
            end = min(int(end_str), total_bytes - 1)

        if end < start:
            return None
        return (start, end)
    except ValueError:
        return None


@router.get(
    "/{layer_id}/flatgeobuf",
    summary="Stream a harmonized cadastral layer as FlatGeobuf with HTTP 206 Range Requests and bbox filtering",
)
async def stream_layer_flatgeobuf(
    layer_id: uuid.UUID,
    bbox: Optional[str] = Query(
        default=None,
        description="Optional WGS 84 bounding box filter: min_lon,min_lat,max_lon,max_lat",
    ),
    range_header: Optional[str] = Header(default=None, alias="Range"),
    session: Optional[AsyncSession] = Depends(get_async_session),
) -> Response:
    """Serialize `layer_id` parcels as FlatGeobuf and serve full (200) or partial (206) byte ranges."""
    parsed_bbox = _parse_wgs84_bbox(bbox)

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable",
        )

    layer_stmt = select(RawCadastralLayer).where(RawCadastralLayer.id == layer_id)
    layer_res = await session.execute(layer_stmt)
    layer = layer_res.scalar_one_or_none()
    if layer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cadastral layer {layer_id} not found",
        )

    parcel_stmt = (
        select(HarmonizedParcel)
        .where(HarmonizedParcel.layer_id == layer_id)
        .order_by(HarmonizedParcel.id)
    )
    parcel_res = await session.execute(parcel_stmt)
    parcels = list(parcel_res.scalars().all())

    if parsed_bbox is not None:
        bbox_geom = shapely_box(*parsed_bbox)
        parcels = [
            p
            for p in parcels
            if p.geometry_wgs84 is not None and to_shape(p.geometry_wgs84).intersects(bbox_geom)
        ]

    fgb_bytes = export_parcels_to_flatgeobuf(
        parcels,
        layer_name=layer.name or str(layer_id),
    )
    total = len(fgb_bytes)
    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{layer_id}.fgb"',
    }

    if range_header is not None:
        byte_range = _parse_byte_range(range_header, total)
        if byte_range is None:
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{total}",
                },
            )
        start, end = byte_range
        slice_bytes = fgb_bytes[start : end + 1]
        return StreamingResponse(
            stream_flatgeobuf_bytes(slice_bytes),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type="application/vnd.flatgeobuf",
            headers={
                **common_headers,
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Length": str(len(slice_bytes)),
            },
        )

    return StreamingResponse(
        stream_flatgeobuf_bytes(fgb_bytes),
        status_code=status.HTTP_200_OK,
        media_type="application/vnd.flatgeobuf",
        headers={
            **common_headers,
            "Content-Length": str(total),
        },
    )


async def _load_layer_and_parcels(
    layer_id: uuid.UUID,
    session: Optional[AsyncSession],
) -> tuple[RawCadastralLayer, list[HarmonizedParcel]]:
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable",
        )

    layer_stmt = select(RawCadastralLayer).where(RawCadastralLayer.id == layer_id)
    layer_res = await session.execute(layer_stmt)
    layer = layer_res.scalar_one_or_none()
    if layer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cadastral layer {layer_id} not found",
        )

    parcel_stmt = (
        select(HarmonizedParcel)
        .where(HarmonizedParcel.layer_id == layer_id)
        .order_by(HarmonizedParcel.id)
    )
    parcel_res = await session.execute(parcel_stmt)
    parcels = list(parcel_res.scalars().all())
    return layer, parcels


@router.get(
    "/{layer_id}/export",
    summary="Download a harmonized cadastral layer as GeoJSON, GeoPackage, or FlatGeobuf",
)
async def download_layer_export(
    layer_id: uuid.UUID,
    format: Literal["geojson", "gpkg", "fgb"] = Query(
        default="geojson",
        description="Export format: geojson, gpkg, or fgb",
    ),
    srid: int = Query(
        default=4326,
        ge=1,
        description="Target SRID for GeoPackage export",
    ),
    include_merged_slivers: bool = Query(
        default=False,
        description="Include merged audit slivers in exported output",
    ),
    session: Optional[AsyncSession] = Depends(get_async_session),
) -> Response:
    """Serialize and stream a harmonized cadastral layer as a downloadable file."""
    layer, parcels = await _load_layer_and_parcels(layer_id, session)
    layer_slug = (layer.name or str(layer_id)).strip().replace(" ", "_").lower() or str(layer_id)
    fmt_meta = SUPPORTED_EXPORT_FORMATS[format]

    if format == "geojson":
        payload = export_parcels_to_geojson_bytes(
            parcels,
            include_merged_slivers=include_merged_slivers,
        )
    elif format == "gpkg":
        try:
            payload = export_parcels_to_geopackage(
                parcels,
                table_name=layer_slug,
                srid=srid,
                include_merged_slivers=include_merged_slivers,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
    else:
        payload = export_parcels_to_flatgeobuf(
            parcels,
            layer_name=layer_slug,
            include_merged_slivers=include_merged_slivers,
        )

    filename = f"{layer_slug}_harmonized.{fmt_meta['ext']}"
    return Response(
        content=payload,
        media_type=fmt_meta["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


@router.post(
    "/{layer_id}/export/presign",
    response_model=LayerExportPresignResponse,
    summary="Stage a harmonized layer export in MinIO and return a presigned download URL",
)
async def presign_layer_export(
    layer_id: uuid.UUID,
    request: LayerExportPresignRequest = Body(default_factory=LayerExportPresignRequest),
    session: Optional[AsyncSession] = Depends(get_async_session),
    storage_client: MinioStorageClient = Depends(get_storage_client),
) -> LayerExportPresignResponse:
    """Stage a harmonized layer export in MinIO and issue a presigned download URL."""
    layer, parcels = await _load_layer_and_parcels(layer_id, session)
    try:
        artifact = stage_harmonized_export(
            layer_id=layer_id,
            layer_name=layer.name or str(layer_id),
            parcels=parcels,
            export_format=request.format,
            storage_client=storage_client,
            srid=request.srid,
            include_merged_slivers=request.include_merged_slivers,
            expires_seconds=request.expires_seconds,
            force_rebuild=request.force_rebuild,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return LayerExportPresignResponse(
        layer_id=str(layer_id),
        format=artifact.format,
        bucket=artifact.bucket,
        object_name=artifact.object_name,
        download_url=artifact.download_url,
        content_type=artifact.content_type,
        byte_size=artifact.byte_size,
        sha256=artifact.sha256,
        feature_count=artifact.feature_count,
        expires_seconds=artifact.expires_seconds,
        cached=artifact.cached,
    )


@router.get(
    "/{layer_id}/export/manifest",
    summary="Get deterministic harmonization audit certificate & lineage manifest for a layer",
)
async def get_layer_export_manifest(
    layer_id: uuid.UUID,
    session: Optional[AsyncSession] = Depends(get_async_session),
) -> dict:
    """Return the deterministic harmonization audit manifest JSON for `layer_id`."""
    layer, parcels = await _load_layer_and_parcels(layer_id, session)
    assert session is not None
    err_stmt = (
        select(TopologicalError)
        .where(TopologicalError.layer_id == layer_id)
        .order_by(TopologicalError.id)
    )
    err_res = await session.execute(err_stmt)
    errors = list(err_res.scalars().all())

    return build_harmonization_audit_manifest(
        layer=layer,
        parcels=parcels,
        topology_errors=errors,
    )




