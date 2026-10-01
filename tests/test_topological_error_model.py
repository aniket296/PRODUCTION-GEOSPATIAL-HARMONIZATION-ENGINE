"""Tests for TopologicalError database model fields, severity types, and geometry fields."""

import uuid
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import inspect

from backend.models.topology import (
    TopologicalError,
    TopologyErrorType,
    TopologySeverity,
)


def test_topological_error_table_name():
    assert TopologicalError.__tablename__ == "topological_errors"


def test_topological_error_foreign_keys():
    mapper = inspect(TopologicalError)
    pk_cols = [c.name for c in mapper.primary_key]
    assert pk_cols == ["id"]

    fk_targets = set()
    for col in mapper.columns:
        for fk in col.foreign_keys:
            fk_targets.add(fk.target_fullname)
    assert "raw_cadastral_layers.id" in fk_targets
    assert "harmonized_parcels.id" in fk_targets


def test_topological_error_geometry_fields():
    table = TopologicalError.__table__
    assert "error_geometry_wgs84" in table.columns
    assert "error_geometry_projected" in table.columns

    wgs84_col = table.columns["error_geometry_wgs84"]
    assert wgs84_col.nullable is False
    assert wgs84_col.type.srid == 4326
    assert wgs84_col.type.geometry_type == "GEOMETRY"
    assert wgs84_col.type.spatial_index is True

    proj_col = table.columns["error_geometry_projected"]
    assert proj_col.nullable is False
    assert proj_col.type.srid == 32643
    assert proj_col.type.geometry_type == "GEOMETRY"
    assert proj_col.type.spatial_index is True


def test_severity_and_error_type_enums():
    # Verify deterministic severity classifications (Section 15 & 21)
    severities = {s.value for s in TopologySeverity}
    assert "AUTO_RESOLVABLE" in severities
    assert "CRITICAL_DISPUTE" in severities
    assert "WARNING" in severities
    assert "INFO" in severities

    # Verify error types (Sections 15, 16, 17)
    types = {t.value for t in TopologyErrorType}
    assert "OVERLAP" in types
    assert "SLIVER" in types
    assert "GAP" in types
    assert "DANGLING_NODE" in types
    assert "DUPLICATE_VERTEX" in types
    assert "INVALID_GEOMETRY" in types


def test_topological_error_metric_columns():
    table = TopologicalError.__table__
    cols = table.columns

    assert "affected_area_sqm" in cols
    assert cols["affected_area_sqm"].nullable is False
    assert cols["affected_area_sqm"].default.arg == 0.0

    assert "overlap_percentage_primary" in cols
    assert cols["overlap_percentage_primary"].nullable is True

    assert "overlap_percentage_secondary" in cols
    assert cols["overlap_percentage_secondary"].nullable is True


def test_resolution_audit_fields():
    table = TopologicalError.__table__
    cols = table.columns

    assert cols["is_resolved"].nullable is False
    assert cols["is_resolved"].default.arg is False

    assert "resolution_method" in cols
    assert "resolved_at" in cols
    assert "resolved_by" in cols
    assert "resolution_details" in cols


def test_topological_error_instantiation():
    layer_id = uuid.uuid4()
    p1_id = uuid.uuid4()
    p2_id = uuid.uuid4()
    overlap_wkt = "POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))"

    error = TopologicalError(
        layer_id=layer_id,
        primary_parcel_id=p1_id,
        secondary_parcel_id=p2_id,
        error_type=TopologyErrorType.OVERLAP.value,
        severity=TopologySeverity.CRITICAL_DISPUTE.value,
        error_geometry_wgs84=WKTElement(overlap_wkt, srid=4326),
        error_geometry_projected=WKTElement(overlap_wkt, srid=32643),
        overlap_percentage_primary=4.5,
        overlap_percentage_secondary=6.2,
        affected_area_sqm=45.2,
        description="Critical overlap exceeding 2% auto-resolution threshold",
    )

    assert error.layer_id == layer_id
    assert error.primary_parcel_id == p1_id
    assert error.secondary_parcel_id == p2_id
    assert error.error_type == "OVERLAP"
    assert error.severity == "CRITICAL_DISPUTE"
    assert error.overlap_percentage_primary == 4.5
    assert error.overlap_percentage_secondary == 6.2
    assert error.affected_area_sqm == 45.2
    assert error.is_resolved is False
