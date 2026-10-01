"""TransformationConfig model representing georeferencing and coordinate transformation parameters."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base

if TYPE_CHECKING:
    from backend.models.layer import RawCadastralLayer


class TransformationType(str, enum.Enum):
    """Supported coordinate transformation and georeferencing algorithms."""

    HELMERT_4P = "HELMERT_4P"
    AFFINE_6P = "AFFINE_6P"
    POLYNOMIAL_2ND = "POLYNOMIAL_2ND"
    THIN_PLATE_SPLINE = "THIN_PLATE_SPLINE"


class TransformationConfig(Base):
    """Database model storing coordinate transformation parameters, GCPs, and residual errors."""

    __tablename__ = "transformation_configs"
    __table_args__ = (
        CheckConstraint(
            "residual_rms_error >= 0",
            name="chk_transformation_residual_rms_non_negative",
        ),
        CheckConstraint(
            "transformation_type IN ('HELMERT_4P', 'AFFINE_6P', 'POLYNOMIAL_2ND', 'THIN_PLATE_SPLINE')",
            name="chk_transformation_type_valid",
        ),
        Index("ix_transformation_configs_layer_id", "layer_id"),
        Index("ix_transformation_configs_is_active", "is_active"),
        Index("ix_transformation_configs_type", "transformation_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    layer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_cadastral_layers.id", ondelete="CASCADE"),
        nullable=False,
    )

    transformation_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=TransformationType.HELMERT_4P.value,
    )
    source_crs: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="LOCAL_CADASTRAL",
    )
    target_crs: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="EPSG:32643",
    )

    # JSONB columns for flexible, structured mathematical transformation state (Section 13 & 14)
    parameters: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    ground_control_points: Mapped[List[Dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )

    # Residual errors
    residual_rms_error: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
    )
    max_residual_error: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
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

    # Relationship
    layer: Mapped[RawCadastralLayer] = relationship(
        "RawCadastralLayer",
        back_populates="transformation_configs",
    )

    def __init__(
        self,
        layer_id: uuid.UUID,
        transformation_type: str = TransformationType.HELMERT_4P.value,
        source_crs: str = "LOCAL_CADASTRAL",
        target_crs: str = "EPSG:32643",
        parameters: Optional[Dict[str, Any]] = None,
        ground_control_points: Optional[List[Dict[str, Any]]] = None,
        residual_rms_error: float = 0.0,
        max_residual_error: Optional[float] = None,
        is_active: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            layer_id=layer_id,
            transformation_type=transformation_type,
            source_crs=source_crs,
            target_crs=target_crs,
            parameters=parameters if parameters is not None else {},
            ground_control_points=ground_control_points if ground_control_points is not None else [],
            residual_rms_error=residual_rms_error,
            max_residual_error=max_residual_error,
            is_active=is_active,
            **kwargs,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize transformation config properties to dictionary."""
        return {
            "id": str(self.id) if self.id else None,
            "layer_id": str(self.layer_id) if self.layer_id else None,
            "transformation_type": self.transformation_type,
            "source_crs": self.source_crs,
            "target_crs": self.target_crs,
            "parameters": self.parameters,
            "ground_control_points": self.ground_control_points,
            "residual_rms_error": self.residual_rms_error,
            "max_residual_error": self.max_residual_error,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<TransformationConfig(id={self.id}, type='{self.transformation_type}', "
            f"source='{self.source_crs}', target='{self.target_crs}', "
            f"rms_error={self.residual_rms_error})>"
        )
