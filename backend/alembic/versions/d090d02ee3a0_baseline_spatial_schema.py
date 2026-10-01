"""baseline_spatial_schema

Revision ID: d090d02ee3a0
Revises: 
Create Date: 2026-10-01 23:23:53.097549

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import geoalchemy2


# revision identifiers, used by Alembic.
revision: str = 'd090d02ee3a0'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Apply baseline spatial database schema for cadastral harmonization."""
    # Ensure spatial and crypto extensions are loaded
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 1. raw_cadastral_layers
    op.create_table(
        "raw_cadastral_layers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_format", sa.String(50), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("source_crs", sa.String(50), nullable=True),
        sa.Column("target_crs", sa.String(50), nullable=False, server_default="EPSG:4326"),
        sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
        sa.Column("feature_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_area_sqm", sa.Float(), nullable=True),
        sa.Column("bounding_box", postgresql.JSONB(), nullable=True),
        sa.Column("bounding_geometry", geoalchemy2.Geometry(geometry_type="POLYGON", srid=4326, spatial_index=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')",
            name="chk_layer_status_valid",
        ),
        sa.CheckConstraint(
            "file_format IN ('DXF', 'SHAPEFILE', 'GEOJSON', 'GEOPACKAGE')",
            name="chk_layer_file_format_valid",
        ),
        sa.CheckConstraint(
            "feature_count >= 0",
            name="chk_layer_feature_count_non_negative",
        ),
    )
    op.create_index("ix_raw_cadastral_layers_status", "raw_cadastral_layers", ["status"])
    op.create_index("ix_raw_cadastral_layers_format", "raw_cadastral_layers", ["file_format"])
    op.create_index("ix_raw_cadastral_layers_created_at", "raw_cadastral_layers", ["created_at"])

    # 2. harmonized_parcels
    op.create_table(
        "harmonized_parcels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("layer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("raw_cadastral_layers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parcel_identifier", sa.String(100), nullable=False),
        sa.Column("original_parcel_id", sa.String(100), nullable=True),
        sa.Column("geometry_wgs84", geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True), nullable=False),
        sa.Column("geometry_projected", geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=32643, spatial_index=True), nullable=False),
        sa.Column("metric_area_sqm", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("metric_perimeter_m", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("polsby_popper_compactness", sa.Float(), nullable=True),
        sa.Column("is_sliver", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_disputed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("dispute_locked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("resolution_status", sa.String(50), nullable=False, server_default="RAW"),
        sa.Column("attributes", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "resolution_status IN ('RAW', 'AUTO_RESOLVED', 'MANUAL_RESOLVED', 'DISPUTED')",
            name="chk_parcel_resolution_status_valid",
        ),
        sa.CheckConstraint("metric_area_sqm >= 0", name="chk_parcel_metric_area_non_negative"),
        sa.CheckConstraint("metric_perimeter_m >= 0", name="chk_parcel_metric_perimeter_non_negative"),
    )
    op.create_index("ix_harmonized_parcels_layer_id", "harmonized_parcels", ["layer_id"])
    op.create_index("ix_harmonized_parcels_parcel_id", "harmonized_parcels", ["parcel_identifier"])
    op.create_index("ix_harmonized_parcels_is_sliver", "harmonized_parcels", ["is_sliver"])
    op.create_index("ix_harmonized_parcels_is_disputed", "harmonized_parcels", ["is_disputed"])
    op.create_index("ix_harmonized_parcels_dispute_locked", "harmonized_parcels", ["dispute_locked"])
    op.create_index("ix_harmonized_parcels_resolution_status", "harmonized_parcels", ["resolution_status"])

    # 3. topological_errors
    op.create_table(
        "topological_errors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("layer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("raw_cadastral_layers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("primary_parcel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("harmonized_parcels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("secondary_parcel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("harmonized_parcels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("error_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("overlap_percentage_primary", sa.Float(), nullable=True),
        sa.Column("overlap_percentage_secondary", sa.Float(), nullable=True),
        sa.Column("affected_area_sqm", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("error_geometry_wgs84", geoalchemy2.Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=True), nullable=False),
        sa.Column("error_geometry_projected", geoalchemy2.Geometry(geometry_type="GEOMETRY", srid=32643, spatial_index=True), nullable=False),
        sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("resolution_method", sa.String(100), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(100), nullable=True),
        sa.Column("resolution_details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("affected_area_sqm >= 0", name="chk_topological_error_affected_area_non_negative"),
        sa.CheckConstraint(
            "severity IN ('AUTO_RESOLVABLE', 'CRITICAL_DISPUTE', 'WARNING', 'INFO')",
            name="chk_topological_error_severity_valid",
        ),
        sa.CheckConstraint(
            "error_type IN ('OVERLAP', 'SLIVER', 'GAP', 'DANGLING_NODE', 'DUPLICATE_VERTEX', 'INVALID_GEOMETRY')",
            name="chk_topological_error_type_valid",
        ),
    )
    op.create_index("ix_topological_errors_layer_id", "topological_errors", ["layer_id"])
    op.create_index("ix_topological_errors_severity", "topological_errors", ["severity"])
    op.create_index("ix_topological_errors_error_type", "topological_errors", ["error_type"])
    op.create_index("ix_topological_errors_is_resolved", "topological_errors", ["is_resolved"])
    op.create_index("ix_topological_errors_primary_parcel", "topological_errors", ["primary_parcel_id"])
    op.create_index("ix_topological_errors_secondary_parcel", "topological_errors", ["secondary_parcel_id"])
    op.create_index("ix_topological_errors_layer_severity", "topological_errors", ["layer_id", "severity"])
    op.create_index("ix_topological_errors_layer_error_type", "topological_errors", ["layer_id", "error_type"])

    # 4. transformation_configs
    op.create_table(
        "transformation_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("layer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("raw_cadastral_layers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("transformation_type", sa.String(50), nullable=False, server_default="HELMERT_4P"),
        sa.Column("source_crs", sa.String(50), nullable=False, server_default="LOCAL_CADASTRAL"),
        sa.Column("target_crs", sa.String(50), nullable=False, server_default="EPSG:32643"),
        sa.Column("parameters", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("ground_control_points", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("residual_rms_error", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("max_residual_error", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("residual_rms_error >= 0", name="chk_transformation_residual_rms_non_negative"),
        sa.CheckConstraint(
            "transformation_type IN ('HELMERT_4P', 'AFFINE_6P', 'POLYNOMIAL_2ND', 'THIN_PLATE_SPLINE')",
            name="chk_transformation_type_valid",
        ),
    )
    op.create_index("ix_transformation_configs_layer_id", "transformation_configs", ["layer_id"])
    op.create_index("ix_transformation_configs_is_active", "transformation_configs", ["is_active"])
    op.create_index("ix_transformation_configs_type", "transformation_configs", ["transformation_type"])
    op.create_index("ix_transformation_configs_layer_active", "transformation_configs", ["layer_id", "is_active"])


def downgrade() -> None:
    """Revert baseline spatial database schema."""
    op.drop_table("transformation_configs")
    op.drop_table("topological_errors")
    op.drop_table("harmonized_parcels")
    op.drop_table("raw_cadastral_layers")
