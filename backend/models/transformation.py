"""TransformationConfig model representing georeferencing and coordinate transformation parameters."""

from __future__ import annotations

import enum
import math
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
        Index("ix_transformation_configs_layer_active", "layer_id", "is_active"),
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

    @staticmethod
    def calculate_rms_error(residuals: List[float]) -> float:
        """Calculate Root Mean Square (RMS) error across a sequence of residuals.

        RMS = sqrt( (1 / N) * sum(r_i^2) )
        """
        if not residuals:
            return 0.0
        sum_sq = sum(float(r) ** 2 for r in residuals)
        return float(round(math.sqrt(sum_sq / len(residuals)), 6))

    @staticmethod
    def validate_gcp_count(transformation_type: str, gcp_count: int) -> bool:
        """Validate whether the number of Ground Control Points satisfies algorithm requirements.

        Minimum Degrees of Freedom:
        - HELMERT_4P: 2 GCPs (4 equations for 4 unknowns)
        - AFFINE_6P: 3 GCPs (6 equations for 6 unknowns)
        - POLYNOMIAL_2ND: 6 GCPs (12 equations for 12 unknowns)
        - THIN_PLATE_SPLINE: 4 GCPs
        """
        min_gcp_map = {
            TransformationType.HELMERT_4P.value: 2,
            TransformationType.AFFINE_6P.value: 3,
            TransformationType.POLYNOMIAL_2ND.value: 6,
            TransformationType.THIN_PLATE_SPLINE.value: 4,
        }
        min_required = min_gcp_map.get(transformation_type, 2)
        return gcp_count >= min_required

    def update_residuals(self, gcps: Optional[List[Dict[str, Any]]] = None) -> None:
        """Recalculate and update residual RMS and maximum errors from GCP residual data."""
        if gcps is not None:
            self.ground_control_points = gcps

        residuals: List[float] = []
        for gcp in self.ground_control_points:
            res = gcp.get("residual_total_m", gcp.get("residual"))
            if res is not None:
                residuals.append(float(res))

        if residuals:
            self.residual_rms_error = self.calculate_rms_error(residuals)
            self.max_residual_error = float(round(max(abs(r) for r in residuals), 6))
        else:
            self.residual_rms_error = 0.0
            self.max_residual_error = 0.0

        self.updated_at = datetime.now(timezone.utc)

    def activate(self) -> None:
        """Mark transformation config as active."""
        self.is_active = True
        self.updated_at = datetime.now(timezone.utc)

    def deactivate(self) -> None:
        """Deactivate transformation config."""
        self.is_active = False
        self.updated_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return (
            f"<TransformationConfig(id={self.id}, type='{self.transformation_type}', "
            f"source='{self.source_crs}', target='{self.target_crs}', "
            f"rms_error={self.residual_rms_error})>"
        )
