"""Tests for TransformationConfig database model and JSONB storage."""

import uuid
import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

from backend.models.transformation import (
    TransformationConfig,
    TransformationType,
)


def test_transformation_config_table_name():
    assert TransformationConfig.__tablename__ == "transformation_configs"


def test_transformation_config_primary_and_foreign_keys():
    mapper = inspect(TransformationConfig)
    pk_cols = [c.name for c in mapper.primary_key]
    assert pk_cols == ["id"]

    fk_targets = set()
    for col in mapper.columns:
        for fk in col.foreign_keys:
            fk_targets.add(fk.target_fullname)
    assert "raw_cadastral_layers.id" in fk_targets


def test_transformation_config_jsonb_columns():
    table = TransformationConfig.__table__
    cols = table.columns

    assert "parameters" in cols
    assert isinstance(cols["parameters"].type, JSONB)
    assert cols["parameters"].nullable is False

    assert "ground_control_points" in cols
    assert isinstance(cols["ground_control_points"].type, JSONB)
    assert cols["ground_control_points"].nullable is False


def test_transformation_type_enums():
    types = {t.value for t in TransformationType}
    assert "HELMERT_4P" in types
    assert "AFFINE_6P" in types
    assert "POLYNOMIAL_2ND" in types
    assert "THIN_PLATE_SPLINE" in types


def test_transformation_config_check_constraints_and_defaults():
    table = TransformationConfig.__table__
    cols = table.columns

    assert cols["is_active"].nullable is False
    assert cols["is_active"].default.arg is True

    assert cols["residual_rms_error"].nullable is False
    assert cols["residual_rms_error"].default.arg == 0.0

    constraint_names = {c.name for c in table.constraints}
    assert "chk_transformation_residual_rms_non_negative" in constraint_names
    assert "chk_transformation_type_valid" in constraint_names


def test_transformation_config_jsonb_storage_helmert_4p():
    layer_id = uuid.uuid4()
    helmert_params = {
        "translation_x_m": 1205.4215,
        "translation_y_m": 834.1923,
        "scale_factor": 1.0000428,
        "rotation_angle_rad": 0.00314159,
        "rotation_angle_deg": 0.18,
    }
    gcps = [
        {
            "id": "GCP_01",
            "source_coords": [500000.0, 2000000.0],
            "target_coords": [501205.42, 2000834.19],
            "residual_x_m": 0.008,
            "residual_y_m": -0.009,
            "residual_total_m": 0.012,
        },
        {
            "id": "GCP_02",
            "source_coords": [500500.0, 2000500.0],
            "target_coords": [501705.43, 2001334.20],
            "residual_x_m": -0.006,
            "residual_y_m": 0.007,
            "residual_total_m": 0.009,
        },
        {
            "id": "GCP_03",
            "source_coords": [500200.0, 2000800.0],
            "target_coords": [501405.41, 2001634.18],
            "residual_x_m": 0.004,
            "residual_y_m": -0.005,
            "residual_total_m": 0.006,
        },
        {
            "id": "GCP_04",
            "source_coords": [500800.0, 2000200.0],
            "target_coords": [502005.44, 2001034.21],
            "residual_x_m": -0.007,
            "residual_y_m": 0.008,
            "residual_total_m": 0.011,
        },
    ]

    config = TransformationConfig(
        layer_id=layer_id,
        transformation_type=TransformationType.HELMERT_4P.value,
        source_crs="EPSG:24378",  # Kalianpur 1975 / India Zone IIa (Everest 1830)
        target_crs="EPSG:32643",  # WGS 84 / UTM Zone 43N
        parameters=helmert_params,
        ground_control_points=gcps,
        residual_rms_error=0.0095,  # Sub-centimeter RMS accuracy
        max_residual_error=0.012,
        is_active=True,
    )

    assert config.layer_id == layer_id
    assert config.transformation_type == "HELMERT_4P"
    assert config.source_crs == "EPSG:24378"
    assert config.target_crs == "EPSG:32643"
    assert config.parameters["translation_x_m"] == 1205.4215
    assert config.parameters["scale_factor"] == 1.0000428
    assert len(config.ground_control_points) == 4
    assert config.ground_control_points[0]["id"] == "GCP_01"
    assert config.residual_rms_error == 0.0095
    assert config.max_residual_error == 0.012
    assert config.is_active is True


def test_transformation_config_jsonb_storage_affine_6p():
    layer_id = uuid.uuid4()
    affine_matrix = {
        "matrix_2x3": [
            [0.9998, -0.0125, 452.12],
            [0.0125, 0.9998, 781.45],
        ],
        "shear_factor": 0.0001,
    }
    gcps = [
        {"id": "GCP_1", "source": [0.0, 0.0], "target": [452.12, 781.45], "residual": 0.002},
        {"id": "GCP_2", "source": [100.0, 0.0], "target": [552.10, 782.70], "residual": 0.004},
        {"id": "GCP_3", "source": [0.0, 100.0], "target": [450.87, 881.43], "residual": 0.003},
    ]

    config = TransformationConfig(
        layer_id=layer_id,
        transformation_type=TransformationType.AFFINE_6P.value,
        source_crs="LOCAL_CADASTRAL",
        target_crs="EPSG:32643",
        parameters=affine_matrix,
        ground_control_points=gcps,
        residual_rms_error=0.003,
    )

    assert config.transformation_type == "AFFINE_6P"
    assert config.parameters["matrix_2x3"][0][0] == 0.9998
    assert len(config.ground_control_points) == 3


def test_transformation_config_to_dict_and_repr():
    layer_id = uuid.uuid4()
    config = TransformationConfig(
        layer_id=layer_id,
        transformation_type=TransformationType.THIN_PLATE_SPLINE.value,
        source_crs="LOCAL_VILLAGE_GRID",
        target_crs="EPSG:32643",
        parameters={"smoothing": 0.0, "num_centers": 8},
        residual_rms_error=0.015,
    )

    d = config.to_dict()
    assert d["layer_id"] == str(layer_id)
    assert d["transformation_type"] == "THIN_PLATE_SPLINE"
    assert d["source_crs"] == "LOCAL_VILLAGE_GRID"
    assert d["parameters"]["num_centers"] == 8
    assert d["residual_rms_error"] == 0.015
    assert d["is_active"] is True

    r = repr(config)
    assert "TransformationConfig" in r
    assert "THIN_PLATE_SPLINE" in r
    assert "LOCAL_VILLAGE_GRID" in r


def test_transformation_config_indexes():
    table = TransformationConfig.__table__
    index_names = {idx.name for idx in table.indexes}

    expected_indexes = {
        "ix_transformation_configs_layer_id",
        "ix_transformation_configs_is_active",
        "ix_transformation_configs_type",
        "ix_transformation_configs_layer_active",
    }
    for expected in expected_indexes:
        assert expected in index_names, f"Index {expected} missing from transformation_configs table"


def test_calculate_rms_error():
    assert TransformationConfig.calculate_rms_error([]) == 0.0

    # residuals = [0.03, 0.04] -> sum_sq = 0.0009 + 0.0016 = 0.0025 -> mean = 0.00125 -> sqrt = 0.035355
    rms = TransformationConfig.calculate_rms_error([0.03, 0.04])
    assert round(rms, 6) == 0.035355

    # Single residual
    assert TransformationConfig.calculate_rms_error([0.05]) == 0.05


def test_validate_gcp_count():
    # Helmert 4P: >= 2
    assert TransformationConfig.validate_gcp_count(TransformationType.HELMERT_4P.value, 1) is False
    assert TransformationConfig.validate_gcp_count(TransformationType.HELMERT_4P.value, 2) is True
    assert TransformationConfig.validate_gcp_count(TransformationType.HELMERT_4P.value, 5) is True

    # Affine 6P: >= 3
    assert TransformationConfig.validate_gcp_count(TransformationType.AFFINE_6P.value, 2) is False
    assert TransformationConfig.validate_gcp_count(TransformationType.AFFINE_6P.value, 3) is True
    assert TransformationConfig.validate_gcp_count(TransformationType.AFFINE_6P.value, 6) is True

    # Polynomial 2nd: >= 6
    assert TransformationConfig.validate_gcp_count(TransformationType.POLYNOMIAL_2ND.value, 5) is False
    assert TransformationConfig.validate_gcp_count(TransformationType.POLYNOMIAL_2ND.value, 6) is True

    # TPS: >= 4
    assert TransformationConfig.validate_gcp_count(TransformationType.THIN_PLATE_SPLINE.value, 3) is False
    assert TransformationConfig.validate_gcp_count(TransformationType.THIN_PLATE_SPLINE.value, 4) is True


def test_update_residuals_calculation():
    layer_id = uuid.uuid4()
    config = TransformationConfig(
        layer_id=layer_id,
        transformation_type=TransformationType.HELMERT_4P.value,
    )
    assert config.residual_rms_error == 0.0
    assert config.max_residual_error is None

    new_gcps = [
        {"id": "GCP_1", "residual_total_m": 0.02},
        {"id": "GCP_2", "residual_total_m": -0.04},
        {"id": "GCP_3", "residual_total_m": 0.03},
    ]
    config.update_residuals(new_gcps)

    # residuals: [0.02, -0.04, 0.03] -> sq: [0.0004, 0.0016, 0.0009] -> sum: 0.0029 -> mean: 0.00096667 -> sqrt: ~0.031091
    assert round(config.residual_rms_error, 4) == 0.0311
    assert config.max_residual_error == 0.04
    assert len(config.ground_control_points) == 3


def test_activation_lifecycle():
    layer_id = uuid.uuid4()
    config = TransformationConfig(
        layer_id=layer_id,
        is_active=True,
    )
    assert config.is_active is True

    config.deactivate()
    assert config.is_active is False

    config.activate()
    assert config.is_active is True

