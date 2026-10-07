"""Pydantic schemas for cadastral layer ingestion and management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class LayerUploadResponse(BaseModel):
    """Response returned upon successfully accepting a layer upload for processing."""

    model_config = ConfigDict(from_attributes=True)

    layer_id: str = Field(description="Unique UUID of the registered cadastral layer")
    job_id: str = Field(description="Celery background task ID assigned to ingestion job")
    status: str = Field(default="PENDING", description="Current lifecycle processing status")
    message: str = Field(
        default="Layer upload accepted for asynchronous processing",
        description="Human-readable processing message",
    )
    filename: str = Field(description="Original uploaded file name")
    file_size_bytes: int = Field(description="Size of the uploaded payload in bytes")
    detected_format: Optional[str] = Field(default=None, description="Auto-detected geospatial format")
    declared_crs: Optional[str] = Field(default=None, description="User declared coordinate reference system")
    target_crs: str = Field(default="EPSG:4326", description="Target harmonization CRS")
    created_at: datetime = Field(description="Timestamp when the upload was registered")


class LayerStatusResponse(BaseModel):
    """Real-time lifecycle processing status and progress for a cadastral layer."""

    model_config = ConfigDict(from_attributes=True)

    layer_id: str = Field(description="Unique UUID of the cadastral layer")
    name: Optional[str] = Field(default=None, description="Cadastral layer name")
    status: str = Field(description="Lifecycle status: PENDING, PROCESSING, COMPLETED, FAILED")
    stage: Optional[str] = Field(default=None, description="Current ingestion pipeline stage")
    progress_percent: int = Field(default=0, ge=0, le=100, description="Pipeline completion percentage (0-100)")
    feature_count: int = Field(default=0, ge=0, description="Extracted feature count")
    detected_crs: Optional[str] = Field(default=None, description="Detected coordinate reference system")
    bounds: Optional[List[float]] = Field(default=None, description="Spatial bounding box [minx, miny, maxx, maxy]")
    error_message: Optional[str] = Field(default=None, description="Error reason if ingestion failed")
    created_at: datetime = Field(description="Layer submission timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="Last status update timestamp")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Additional extraction and validation metadata")

