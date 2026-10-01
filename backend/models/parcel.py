"""HarmonizedParcel model representing standardized, projected cadastral parcels.

Implements projected metric spatial operations, Polsby-Popper compactness,
deterministic sliver detection, dispute locking, and RFC 7946 GeoJSON export.
"""

from __future__ import annotations

import enum
import math
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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base

if TYPE_CHECKING:
    from backend.models.layer import RawCadastralLayer


class ParcelResolutionStatus(str, enum.Enum):
    """Harmonization and resolution status for cadastral parcel boundaries."""

    ORIGINAL = "ORIGINAL"
    AUTO_RESOLVED = "AUTO_RESOLVED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DISPUTED = "DISPUTED"


class HarmonizedParcel(Base):
    """Harmonized parcel with projected metric geometries and topology tracking."""

    __tablename__ = "harmonized_parcels"
    __table_args__ = (
        CheckConstraint(
            "metric_area_sqm >= 0",
            name="chk_parcel_metric_area_non_negative",
        ),
        CheckConstraint(
            "metric_perimeter_m >= 0",
            name="chk_parcel_metric_perimeter_non_negative",
        ),
        Index("ix_harmonized_parcels_layer_id", "layer_id"),
        Index("ix_harmonized_parcels_parcel_id", "parcel_identifier"),
        Index("ix_harmonized_parcels_is_sliver", "is_sliver"),
        Index("ix_harmonized_parcels_is_disputed", "is_disputed"),
        Index("ix_harmonized_parcels_dispute_locked", "dispute_locked"),
        Index("ix_harmonized_parcels_resolution_status", "resolution_status"),
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
    parcel_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    original_parcel_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Canonical WGS 84 (EPSG:4326) geometry for visualization and exchange
    geometry_wgs84: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True),
        nullable=False,
    )

    # Metric projected geometry (default UTM Zone 43N EPSG:32643) for accurate spatial operations
    geometry_projected: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=32643, spatial_index=True),
        nullable=False,
    )

    # Generated and stored metric properties in projected coordinate reference system
    metric_area_sqm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    metric_perimeter_m: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    polsby_popper_compactness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Topological classification flags (Sections 15, 16, 21)
    is_sliver: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_disputed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispute_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    resolution_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=ParcelResolutionStatus.ORIGINAL.value,
    )

    # Arbitrary source attribute table data preserved in JSONB
    attributes: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

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

    # Relationship to parent layer
    layer: Mapped[RawCadastralLayer] = relationship(
        "RawCadastralLayer",
        back_populates="parcels",
    )

    def __init__(
        self,
        layer_id: uuid.UUID,
        parcel_identifier: str,
        geometry_wgs84: Any,
        geometry_projected: Any,
        original_parcel_id: Optional[str] = None,
        metric_area_sqm: float = 0.0,
        metric_perimeter_m: float = 0.0,
        polsby_popper_compactness: float = 0.0,
        is_sliver: bool = False,
        is_disputed: bool = False,
        dispute_locked: bool = False,
        resolution_status: str = ParcelResolutionStatus.ORIGINAL.value,
        attributes: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            layer_id=layer_id,
            parcel_identifier=parcel_identifier,
            geometry_wgs84=geometry_wgs84,
            geometry_projected=geometry_projected,
            original_parcel_id=original_parcel_id,
            metric_area_sqm=metric_area_sqm,
            metric_perimeter_m=metric_perimeter_m,
            polsby_popper_compactness=polsby_popper_compactness,
            is_sliver=is_sliver,
            is_disputed=is_disputed,
            dispute_locked=dispute_locked,
            resolution_status=resolution_status,
            attributes=attributes,
            **kwargs,
        )

    @staticmethod
    def calculate_polsby_popper(area_sqm: float, perimeter_m: float) -> float:
        """Calculate Polsby-Popper compactness: 4 * pi * Area / (Perimeter ^ 2).

        Returns 0.0 if perimeter is non-positive. Maximum theoretical value is 1.0 (circle).
        """
        if perimeter_m <= 0 or area_sqm <= 0:
            return 0.0
        return (4.0 * math.pi * area_sqm) / (perimeter_m * perimeter_m)

    def recalculate_metrics(self, shapely_projected_geom: Optional[Any] = None) -> None:
        """Recalculate metric area, perimeter, Polsby-Popper compactness, and sliver status."""
        geom = shapely_projected_geom
        if geom is None and self.geometry_projected is not None:
            try:
                geom = to_shape(self.geometry_projected)
            except Exception:
                geom = None

        if geom is not None and not geom.is_empty:
            self.metric_area_sqm = float(round(geom.area, 4))
            self.metric_perimeter_m = float(round(geom.length, 4))
            self.polsby_popper_compactness = float(
                round(
                    self.calculate_polsby_popper(self.metric_area_sqm, self.metric_perimeter_m),
                    6,
                )
            )
            # Section 16: area < 10 sqm AND Polsby-Popper < 0.05
            self.is_sliver = bool(
                self.metric_area_sqm < 10.0 and self.polsby_popper_compactness < 0.05
            )
        self.updated_at = datetime.now(timezone.utc)

    def lock_dispute(self, reason: Optional[str] = None) -> None:
        """Lock parcel boundary due to critical dispute (> 2% overlap).

        Prevents automatic healing and preserves provenance.
        """
        self.is_disputed = True
        self.dispute_locked = True
        self.resolution_status = ParcelResolutionStatus.DISPUTED.value
        if reason:
            attrs = dict(self.attributes or {})
            attrs["dispute_reason"] = reason
            self.attributes = attrs
        self.updated_at = datetime.now(timezone.utc)

    def mark_auto_resolved(self) -> None:
        """Mark parcel as successfully healed via deterministic micro-overlap or sliver union."""
        if self.dispute_locked:
            raise ValueError(
                f"Cannot automatically resolve parcel {self.parcel_identifier}: Boundary is dispute locked."
            )
        self.resolution_status = ParcelResolutionStatus.AUTO_RESOLVED.value
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize parcel metadata and metric properties to dictionary."""
        return {
            "id": str(self.id) if self.id else None,
            "layer_id": str(self.layer_id) if self.layer_id else None,
            "parcel_identifier": self.parcel_identifier,
            "original_parcel_id": self.original_parcel_id,
            "metric_area_sqm": self.metric_area_sqm,
            "metric_perimeter_m": self.metric_perimeter_m,
            "polsby_popper_compactness": self.polsby_popper_compactness,
            "is_sliver": self.is_sliver,
            "is_disputed": self.is_disputed,
            "dispute_locked": self.dispute_locked,
            "resolution_status": self.resolution_status,
            "attributes": self.attributes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_geojson_feature(self) -> Dict[str, Any]:
        """Generate an RFC 7946 compliant GeoJSON Feature dictionary."""
        geom_dict = None
        if self.geometry_wgs84 is not None:
            try:
                geom_dict = mapping(to_shape(self.geometry_wgs84))
            except Exception:
                geom_dict = None

        return {
            "type": "Feature",
            "id": str(self.id) if self.id else None,
            "geometry": geom_dict,
            "properties": self.to_dict(),
        }

    def __repr__(self) -> str:
        return (
            f"<HarmonizedParcel id={self.id} identifier='{self.parcel_identifier}' "
            f"area={self.metric_area_sqm:.2f}m² sliver={self.is_sliver} disputed={self.is_disputed}>"
        )
