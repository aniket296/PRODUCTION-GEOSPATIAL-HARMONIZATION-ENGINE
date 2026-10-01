"""Tests for RawCadastralLayer database model fields, constraints, and defaults."""

import uuid
import pytest
from sqlalchemy import inspect
from backend.models.layer import LayerStatus, RawCadastralLayer, SupportedFormat


def test_table_name():
    assert RawCadastralLayer.__tablename__ == "raw_cadastral_layers"


def test_primary_key_constraint():
    mapper = inspect(RawCadastralLayer)
    pk_cols = [c.name for c in mapper.primary_key]
    assert pk_cols == ["id"]


def test_required_columns_exist():
    mapper = inspect(RawCadastralLayer)
    col_names = {c.name for c in mapper.columns}
    expected = {
        "id",
        "name",
        "filename",
        "file_format",
        "storage_path",
        "source_crs",
        "target_crs",
        "status",
        "error_message",
        "feature_count",
        "total_area_sqm",
        "bounding_box",
        "metadata_json",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(col_names)


def test_column_nullability_constraints():
    mapper = inspect(RawCadastralLayer)
    cols = {c.name: c for c in mapper.columns}

    # Non-nullable columns
    assert cols["id"].nullable is False
    assert cols["name"].nullable is False
    assert cols["filename"].nullable is False
    assert cols["file_format"].nullable is False
    assert cols["storage_path"].nullable is False
    assert cols["target_crs"].nullable is False
    assert cols["status"].nullable is False
    assert cols["feature_count"].nullable is False
    assert cols["created_at"].nullable is False
    assert cols["updated_at"].nullable is False

    # Nullable columns
    assert cols["source_crs"].nullable is True
    assert cols["error_message"].nullable is True
    assert cols["total_area_sqm"].nullable is True
    assert cols["bounding_box"].nullable is True
    assert cols["metadata_json"].nullable is True


def test_column_defaults():
    mapper = inspect(RawCadastralLayer)
    cols = {c.name: c for c in mapper.columns}

    assert cols["target_crs"].default.arg == "EPSG:4326"
    assert cols["status"].default.arg == LayerStatus.PENDING.value
    assert cols["feature_count"].default.arg == 0


def test_supported_formats_and_statuses():
    assert "SHAPEFILE" in [f.value for f in SupportedFormat]
    assert "GEOJSON" in [f.value for f in SupportedFormat]
    assert "KML" in [f.value for f in SupportedFormat]
    assert "KMZ" in [f.value for f in SupportedFormat]
    assert "DXF" in [f.value for f in SupportedFormat]

    assert "PENDING" in [s.value for s in LayerStatus]
    assert "COMPLETED" in [s.value for s in LayerStatus]
    assert "FAILED" in [s.value for s in LayerStatus]


def test_model_instantiation():
    layer = RawCadastralLayer(
        name="Ward 12 Cadastral Survey",
        filename="ward_12_survey.shp",
        file_format=SupportedFormat.SHAPEFILE.value,
        storage_path="cadastral-raw/2026/ward_12_survey.zip",
        source_crs="EPSG:32643",
    )
    assert layer.name == "Ward 12 Cadastral Survey"
    assert layer.filename == "ward_12_survey.shp"
    assert layer.file_format == "SHAPEFILE"
    assert layer.storage_path == "cadastral-raw/2026/ward_12_survey.zip"
    assert layer.source_crs == "EPSG:32643"
    assert layer.target_crs == "EPSG:4326"
    assert layer.status == "PENDING"
    assert layer.feature_count == 0


def test_table_indexes_and_check_constraints():
    table = RawCadastralLayer.__table__

    # Verify check constraints
    check_names = {c.name for c in table.constraints if c.name}
    assert "chk_raw_cadastral_layer_feature_count_non_negative" in check_names
    assert "chk_raw_cadastral_layer_status_valid" in check_names

    # Verify indexes
    index_names = {idx.name for idx in table.indexes}
    assert "ix_raw_cadastral_layers_status" in index_names
    assert "ix_raw_cadastral_layers_created_at" in index_names


def test_bounding_geometry_column():
    table = RawCadastralLayer.__table__
    assert "bounding_geometry" in table.columns
    geom_col = table.columns["bounding_geometry"]
    assert geom_col.nullable is True
    assert geom_col.type.srid == 4326
    assert geom_col.type.geometry_type == "POLYGON"


def test_state_transitions():
    layer = RawCadastralLayer(
        name="Ward 12",
        filename="w12.geojson",
        file_format=SupportedFormat.GEOJSON.value,
        storage_path="raw/w12.geojson",
    )
    assert layer.status == LayerStatus.PENDING.value

    # Transition to processing
    layer.mark_processing()
    assert layer.status == LayerStatus.PROCESSING.value
    assert layer.error_message is None

    # Transition to completed
    layer.mark_completed(feature_count=150, total_area_sqm=125000.5, bounding_box={"minx": 77.0})
    assert layer.status == LayerStatus.COMPLETED.value
    assert layer.feature_count == 150
    assert layer.total_area_sqm == 125000.5
    assert layer.bounding_box == {"minx": 77.0}

    # Transition to failed
    layer.mark_failed("Corrupt polygon topology encountered")
    assert layer.status == LayerStatus.FAILED.value
    assert layer.error_message == "Corrupt polygon topology encountered"


def test_to_dict_and_repr():
    layer = RawCadastralLayer(
        name="Ward 12",
        filename="w12.geojson",
        file_format=SupportedFormat.GEOJSON.value,
        storage_path="raw/w12.geojson",
    )
    data = layer.to_dict()
    assert isinstance(data, dict)
    assert data["name"] == "Ward 12"
    assert data["status"] == "PENDING"
    assert "w12.geojson" in str(layer)
