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


def test_topological_error_indexes():
    table = TopologicalError.__table__
    index_names = {idx.name for idx in table.indexes}

    expected_indexes = {
        "ix_topological_errors_layer_id",
        "ix_topological_errors_severity",
        "ix_topological_errors_error_type",
        "ix_topological_errors_is_resolved",
        "ix_topological_errors_primary_parcel",
        "ix_topological_errors_secondary_parcel",
        "ix_topological_errors_layer_severity",
        "ix_topological_errors_layer_error_type",
    }
    for expected in expected_indexes:
        assert expected in index_names, f"Index {expected} missing from topological_errors table"


def test_classify_overlap_severity():
    # <= 2.0% on both -> AUTO_RESOLVABLE
    assert (
        TopologicalError.classify_overlap_severity(1.5, 1.8)
        == TopologySeverity.AUTO_RESOLVABLE
    )
    assert (
        TopologicalError.classify_overlap_severity(2.0, 2.0)
        == TopologySeverity.AUTO_RESOLVABLE
    )
    assert (
        TopologicalError.classify_overlap_severity(None, None)
        == TopologySeverity.AUTO_RESOLVABLE
    )

    # > 2.0% on either parcel -> CRITICAL_DISPUTE
    assert (
        TopologicalError.classify_overlap_severity(2.05, 1.0)
        == TopologySeverity.CRITICAL_DISPUTE
    )
    assert (
        TopologicalError.classify_overlap_severity(0.5, 3.2)
        == TopologySeverity.CRITICAL_DISPUTE
    )
    assert (
        TopologicalError.classify_overlap_severity(5.0, 5.0)
        == TopologySeverity.CRITICAL_DISPUTE
    )


def test_topological_error_resolve():
    layer_id = uuid.uuid4()
    error = TopologicalError(
        layer_id=layer_id,
        error_type=TopologyErrorType.SLIVER.value,
        severity=TopologySeverity.AUTO_RESOLVABLE.value,
        error_geometry_wgs84=WKTElement("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))", srid=4326),
        error_geometry_projected=WKTElement("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))", srid=32643),
    )
    assert error.is_resolved is False
    assert error.resolution_method is None

    error.resolve(
        method="SLIVER_MERGE_LONGEST_EDGE",
        resolved_by="system_auto_healer",
        details={"merged_into_parcel": "P-101", "shared_boundary_length_m": 12.4},
    )

    assert error.is_resolved is True
    assert error.resolution_method == "SLIVER_MERGE_LONGEST_EDGE"
    assert error.resolved_by == "system_auto_healer"
    assert error.resolved_at is not None
    assert error.resolution_details["merged_into_parcel"] == "P-101"


def test_to_dict_and_repr():
    layer_id = uuid.uuid4()
    p1 = uuid.uuid4()
    error = TopologicalError(
        layer_id=layer_id,
        primary_parcel_id=p1,
        error_type=TopologyErrorType.GAP.value,
        severity=TopologySeverity.WARNING.value,
        error_geometry_wgs84=WKTElement("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))", srid=4326),
        error_geometry_projected=WKTElement("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))", srid=32643),
        affected_area_sqm=3.5,
    )

    d = error.to_dict()
    assert d["layer_id"] == str(layer_id)
    assert d["primary_parcel_id"] == str(p1)
    assert d["error_type"] == "GAP"
    assert d["severity"] == "WARNING"
    assert d["affected_area_sqm"] == 3.5
    assert d["is_resolved"] is False

    r = repr(error)
    assert "TopologicalError" in r
    assert "GAP" in r
    assert "WARNING" in r


def test_to_geojson_feature():
    layer_id = uuid.uuid4()
    wkt = "POLYGON((77.59 12.97, 77.60 12.97, 77.60 12.98, 77.59 12.98, 77.59 12.97))"
    error = TopologicalError(
        layer_id=layer_id,
        error_type=TopologyErrorType.OVERLAP.value,
        severity=TopologySeverity.CRITICAL_DISPUTE.value,
        error_geometry_wgs84=WKTElement(wkt, srid=4326),
        error_geometry_projected=WKTElement(wkt, srid=32643),
        overlap_percentage_primary=3.5,
        affected_area_sqm=120.0,
    )

    feat = error.to_geojson_feature()
    assert feat["type"] == "Feature"
    assert feat["geometry"] is not None
    assert feat["geometry"]["type"] == "Polygon"
    assert feat["properties"]["error_type"] == "OVERLAP"
    assert feat["properties"]["severity"] == "CRITICAL_DISPUTE"
    assert feat["properties"]["overlap_percentage_primary"] == 3.5
    assert feat["properties"]["affected_area_sqm"] == 120.0

