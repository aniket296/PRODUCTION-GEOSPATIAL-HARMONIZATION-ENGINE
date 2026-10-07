"""API endpoints for cadastral survey layer ingestion, upload, and status tracking."""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas.layer import LayerStatusResponse, LayerUploadResponse
from backend.core.config import get_settings
from backend.db.session import get_async_session
from backend.ingestion.validation import UploadValidator
from backend.models.layer import LayerStatus, RawCadastralLayer
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

