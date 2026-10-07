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
    declared_crs: Optional[str] = Field(default=None, description="User declared coordinate reference system")
    target_crs: str = Field(default="EPSG:4326", description="Target harmonization CRS")
    created_at: datetime = Field(description="Timestamp when the upload was registered")
