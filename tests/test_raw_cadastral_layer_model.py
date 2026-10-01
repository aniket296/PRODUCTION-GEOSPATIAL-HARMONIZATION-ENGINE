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
