"""RawCadastralLayer model representing unharmonized source cadastral spatial layers.

Provides metadata tracking, provenance, PostGIS spatial indexing, and lifecycle status management.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from geoalchemy2 import Geometry
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base

if TYPE_CHECKING:
    from backend.models.parcel import HarmonizedParcel
    from backend.models.topology import TopologicalError
    from backend.models.transformation import TransformationConfig


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
    FLATGEOBUF = "FLATGEOBUF"
    GEOTIFF = "GEOTIFF"


class RawCadastralLayer(Base):
    """Raw uploaded cadastral layer representing the unharmonized source geometry."""

    __tablename__ = "raw_cadastral_layers"
    __table_args__ = (
        CheckConstraint(
            "feature_count >= 0",
            name="chk_raw_cadastral_layer_feature_count_non_negative",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'UPLOADING', 'PROCESSING', 'CALIBRATED', 'COMPLETED', 'FAILED')",
            name="chk_raw_cadastral_layer_status_valid",
        ),
        Index("ix_raw_cadastral_layers_status", "status"),
        Index("ix_raw_cadastral_layers_created_at", "created_at"),
    )

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

    # PostGIS bounding polygon geometry (WGS 84 EPSG:4326) with GiST spatial index
    bounding_geometry: Mapped[Optional[Any]] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )

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

    # Relationships to child entities
    parcels: Mapped[List["HarmonizedParcel"]] = relationship(
        "HarmonizedParcel",
        back_populates="layer",
        cascade="all, delete-orphan",
    )
    topology_errors: Mapped[List["TopologicalError"]] = relationship(
        "TopologicalError",
        back_populates="layer",
        cascade="all, delete-orphan",
    )
    transformation_configs: Mapped[List["TransformationConfig"]] = relationship(
        "TransformationConfig",
        back_populates="layer",
        cascade="all, delete-orphan",
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

    def mark_processing(self) -> None:
        """Transition layer status to PROCESSING."""
        self.status = LayerStatus.PROCESSING.value
        self.error_message = None
        self.updated_at = datetime.now(timezone.utc)

    def mark_completed(
        self,
        feature_count: int,
        total_area_sqm: Optional[float] = None,
        bounding_box: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark layer as successfully processed."""
        self.status = LayerStatus.COMPLETED.value
        self.feature_count = feature_count
        self.total_area_sqm = total_area_sqm
        self.bounding_box = bounding_box
        self.error_message = None
        self.updated_at = datetime.now(timezone.utc)

    def mark_failed(self, error_message: str) -> None:
        """Mark layer as failed with descriptive diagnostics."""
        self.status = LayerStatus.FAILED.value
        self.error_message = error_message
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize layer metadata to dictionary."""
        return {
            "id": str(self.id) if self.id else None,
            "name": self.name,
            "filename": self.filename,
            "file_format": self.file_format,
            "storage_path": self.storage_path,
            "source_crs": self.source_crs,
            "target_crs": self.target_crs,
            "status": self.status,
            "error_message": self.error_message,
            "feature_count": self.feature_count,
            "total_area_sqm": self.total_area_sqm,
            "bounding_box": self.bounding_box,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<RawCadastralLayer id={self.id} name='{self.name}' filename='{self.filename}' "
            f"format='{self.file_format}' status='{self.status}' count={self.feature_count}>"
        )
