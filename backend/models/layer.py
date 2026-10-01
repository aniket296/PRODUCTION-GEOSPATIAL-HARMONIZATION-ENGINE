"""RawCadastralLayer model schema and field definitions."""

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


class LayerStatus(str, enum.Enum):
    """Lifecycle status of an uploaded raw cadastral layer."""

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    CALIBRATED = "CALIBRATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SupportedFormat(str, enum.Enum):
    """Supported input vector geospatial formats."""

    SHAPEFILE = "SHAPEFILE"
    GEOJSON = "GEOJSON"
    KML = "KML"
    KMZ = "KMZ"
    DXF = "DXF"


class RawCadastralLayer(Base):
    """Raw uploaded cadastral layer representing the unharmonized source geometry."""

    __tablename__ = "raw_cadastral_layers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_format: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_crs: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    target_crs: Mapped[str] = mapped_column(String(100), nullable=False, default="EPSG:4326")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default=LayerStatus.PENDING.value)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    feature_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_area_sqm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bounding_box: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __init__(
        self,
        name: str,
        filename: str,
        file_format: str,
        storage_path: str,
        source_crs: Optional[str] = None,
        target_crs: str = "EPSG:4326",
        status: str = LayerStatus.PENDING.value,
        feature_count: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=name,
            filename=filename,
            file_format=file_format,
            storage_path=storage_path,
            source_crs=source_crs,
            target_crs=target_crs,
            status=status,
            feature_count=feature_count,
            **kwargs,
        )
