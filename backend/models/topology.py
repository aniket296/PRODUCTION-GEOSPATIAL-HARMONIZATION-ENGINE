"""TopologicalError model representing detected spatial anomalies and boundary conflicts."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from geoalchemy2 import Geometry
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base

if TYPE_CHECKING:
    from backend.models.layer import RawCadastralLayer
    from backend.models.parcel import HarmonizedParcel


class TopologyErrorType(str, enum.Enum):
    """Categorization of detected cadastral topological anomalies."""

    OVERLAP = "OVERLAP"
    SLIVER = "SLIVER"
    GAP = "GAP"
    DANGLING_NODE = "DANGLING_NODE"
    DUPLICATE_VERTEX = "DUPLICATE_VERTEX"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"


class TopologySeverity(str, enum.Enum):
    """Deterministic severity classification determining automated healing eligibility."""

    AUTO_RESOLVABLE = "AUTO_RESOLVABLE"
    CRITICAL_DISPUTE = "CRITICAL_DISPUTE"
    WARNING = "WARNING"
    INFO = "INFO"


class TopologicalError(Base):
    """Topological conflict record preserving defect geometry, severity, and resolution state."""

    __tablename__ = "topological_errors"
    __table_args__ = (
        CheckConstraint(
            "affected_area_sqm >= 0",
            name="chk_topological_error_affected_area_non_negative",
        ),
        CheckConstraint(
            "severity IN ('AUTO_RESOLVABLE', 'CRITICAL_DISPUTE', 'WARNING', 'INFO')",
            name="chk_topological_error_severity_valid",
        ),
        CheckConstraint(
            "error_type IN ('OVERLAP', 'SLIVER', 'GAP', 'DANGLING_NODE', 'DUPLICATE_VERTEX', 'INVALID_GEOMETRY')",
            name="chk_topological_error_type_valid",
        ),
        Index("ix_topological_errors_layer_id", "layer_id"),
        Index("ix_topological_errors_severity", "severity"),
        Index("ix_topological_errors_error_type", "error_type"),
        Index("ix_topological_errors_is_resolved", "is_resolved"),
        Index("ix_topological_errors_primary_parcel", "primary_parcel_id"),
        Index("ix_topological_errors_secondary_parcel", "secondary_parcel_id"),
        Index("ix_topological_errors_layer_severity", "layer_id", "severity"),
        Index("ix_topological_errors_layer_error_type", "layer_id", "error_type"),
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
    primary_parcel_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("harmonized_parcels.id", ondelete="SET NULL"),
        nullable=True,
    )
    secondary_parcel_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("harmonized_parcels.id", ondelete="SET NULL"),
        nullable=True,
    )

    error_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Overlap percentages independently calculated against each parcel (Section 15)
    overlap_percentage_primary: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    overlap_percentage_secondary: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    affected_area_sqm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Conflict geometry (can be Polygon, MultiPolygon, LineString, or Point)
    error_geometry_wgs84: Mapped[Any] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=True),
        nullable=False,
    )
    error_geometry_projected: Mapped[Any] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=32643, spatial_index=True),
        nullable=False,
    )

    # Resolution tracking & auditability (Section 22)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_method: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resolution_details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

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

    # Relationships
    layer: Mapped[RawCadastralLayer] = relationship(
        "RawCadastralLayer",
        back_populates="topology_errors",
    )

    def __init__(
        self,
        layer_id: uuid.UUID,
        error_type: str,
        severity: str,
        error_geometry_wgs84: Any,
        error_geometry_projected: Any,
        primary_parcel_id: Optional[uuid.UUID] = None,
        secondary_parcel_id: Optional[uuid.UUID] = None,
        description: Optional[str] = None,
        overlap_percentage_primary: Optional[float] = None,
        overlap_percentage_secondary: Optional[float] = None,
        affected_area_sqm: float = 0.0,
        is_resolved: bool = False,
        resolution_method: Optional[str] = None,
        resolved_at: Optional[datetime] = None,
        resolved_by: Optional[str] = None,
        resolution_details: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            layer_id=layer_id,
            error_type=error_type,
            severity=severity,
            error_geometry_wgs84=error_geometry_wgs84,
            error_geometry_projected=error_geometry_projected,
            primary_parcel_id=primary_parcel_id,
            secondary_parcel_id=secondary_parcel_id,
            description=description,
            overlap_percentage_primary=overlap_percentage_primary,
            overlap_percentage_secondary=overlap_percentage_secondary,
            affected_area_sqm=affected_area_sqm,
            is_resolved=is_resolved,
            resolution_method=resolution_method,
            resolved_at=resolved_at,
            resolved_by=resolved_by,
            resolution_details=resolution_details,
            **kwargs,
        )

    @classmethod
    def classify_overlap_severity(
        cls,
        overlap_pct_primary: Optional[float],
        overlap_pct_secondary: Optional[float],
        threshold: float = 2.0,
    ) -> TopologySeverity:
        """Classify overlap severity deterministically based on threshold (Section 15).

        Overlap <= 2.0% on both parcels is AUTO_RESOLVABLE.
        Overlap > 2.0% on either parcel is CRITICAL_DISPUTE requiring manual review.
        """
        pct1 = overlap_pct_primary or 0.0
        pct2 = overlap_pct_secondary or 0.0
        if pct1 > threshold or pct2 > threshold:
            return TopologySeverity.CRITICAL_DISPUTE
        return TopologySeverity.AUTO_RESOLVABLE

    def resolve(
        self,
        method: str,
        resolved_by: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark error as resolved with provenance audit trail."""
        self.is_resolved = True
        self.resolution_method = method
        self.resolved_by = resolved_by
        self.resolved_at = datetime.now(timezone.utc)
        self.resolution_details = details
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize topological error attributes to dictionary."""
        return {
            "id": str(self.id) if self.id else None,
            "layer_id": str(self.layer_id) if self.layer_id else None,
            "primary_parcel_id": str(self.primary_parcel_id) if self.primary_parcel_id else None,
            "secondary_parcel_id": str(self.secondary_parcel_id) if self.secondary_parcel_id else None,
            "error_type": self.error_type,
            "severity": self.severity,
            "description": self.description,
            "overlap_percentage_primary": self.overlap_percentage_primary,
            "overlap_percentage_secondary": self.overlap_percentage_secondary,
            "affected_area_sqm": self.affected_area_sqm,
            "is_resolved": self.is_resolved,
            "resolution_method": self.resolution_method,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolution_details": self.resolution_details,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_geojson_feature(self) -> Dict[str, Any]:
        """Generate an RFC 7946 compliant GeoJSON Feature dictionary."""
        geom_dict = None
        if self.error_geometry_wgs84 is not None:
            try:
                geom_dict = mapping(to_shape(self.error_geometry_wgs84))
            except Exception:
                geom_dict = None

        return {
            "type": "Feature",
            "id": str(self.id) if self.id else None,
            "geometry": geom_dict,
            "properties": {
                "layer_id": str(self.layer_id) if self.layer_id else None,
                "primary_parcel_id": str(self.primary_parcel_id) if self.primary_parcel_id else None,
                "secondary_parcel_id": str(self.secondary_parcel_id) if self.secondary_parcel_id else None,
                "error_type": self.error_type,
                "severity": self.severity,
                "description": self.description,
                "overlap_percentage_primary": self.overlap_percentage_primary,
                "overlap_percentage_secondary": self.overlap_percentage_secondary,
                "affected_area_sqm": self.affected_area_sqm,
                "is_resolved": self.is_resolved,
                "resolution_method": self.resolution_method,
                "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
                "resolved_by": self.resolved_by,
                "resolution_details": self.resolution_details,
            },
        }

    def __repr__(self) -> str:
        return (
            f"<TopologicalError(id={self.id}, type='{self.error_type}', "
            f"severity='{self.severity}', is_resolved={self.is_resolved})>"
        )

