"""Tests for HarmonizedParcel geometry fields, generated metric columns, and topology flags."""

import math
import uuid
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import inspect

from backend.models.layer import RawCadastralLayer, SupportedFormat
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus


def test_parcel_table_name():
    assert HarmonizedParcel.__tablename__ == "harmonized_parcels"


def test_parcel_primary_and_foreign_keys():
    mapper = inspect(HarmonizedParcel)
    pk_cols = [c.name for c in mapper.primary_key]
    assert pk_cols == ["id"]

    fk_targets = set()
    for col in mapper.columns:
        for fk in col.foreign_keys:
            fk_targets.add(fk.target_fullname)
    assert "raw_cadastral_layers.id" in fk_targets


def test_parcel_geometry_fields():
    table = HarmonizedParcel.__table__
    assert "geometry_wgs84" in table.columns
    assert "geometry_projected" in table.columns

    wgs84_col = table.columns["geometry_wgs84"]
    assert wgs84_col.nullable is False
    assert wgs84_col.type.srid == 4326
    assert wgs84_col.type.geometry_type == "MULTIPOLYGON"
    assert wgs84_col.type.spatial_index is True

    proj_col = table.columns["geometry_projected"]
    assert proj_col.nullable is False
    assert proj_col.type.srid == 32643
    assert proj_col.type.geometry_type == "MULTIPOLYGON"
    assert proj_col.type.spatial_index is True


def test_parcel_generated_metric_columns():
    table = HarmonizedParcel.__table__
    cols = table.columns

    # Check metric columns
    assert "metric_area_sqm" in cols
    assert cols["metric_area_sqm"].nullable is False
    assert cols["metric_area_sqm"].default.arg == 0.0

    assert "metric_perimeter_m" in cols
    assert cols["metric_perimeter_m"].nullable is False
    assert cols["metric_perimeter_m"].default.arg == 0.0

    assert "polsby_popper_compactness" in cols
    assert cols["polsby_popper_compactness"].nullable is False
    assert cols["polsby_popper_compactness"].default.arg == 0.0

    # Check topological flags
    assert cols["is_sliver"].nullable is False
    assert cols["is_sliver"].default.arg is False

    assert cols["is_disputed"].nullable is False
    assert cols["is_disputed"].default.arg is False

    assert cols["dispute_locked"].nullable is False
    assert cols["dispute_locked"].default.arg is False

    assert cols["resolution_status"].default.arg == ParcelResolutionStatus.ORIGINAL.value


def test_polsby_popper_compactness_calculation():
    calc = HarmonizedParcel.calculate_polsby_popper

    # Circle (r = 10): 4 * pi * (pi * 100) / (2 * pi * 10)^2 == 1.0
    r = 10.0
    circle_area = math.pi * (r ** 2)
    circle_perimeter = 2.0 * math.pi * r
    assert math.isclose(calc(circle_area, circle_perimeter), 1.0, rel_tol=1e-5)

    # Square (side = 10): Area = 100, Perimeter = 40 => 4 * pi * 100 / 1600 = pi / 4 ~= 0.785398
    square_area = 100.0
    square_perimeter = 40.0
    assert math.isclose(calc(square_area, square_perimeter), math.pi / 4.0, rel_tol=1e-5)

    # Elongated sliver polygon candidate (Area = 8 m^2 < 10 m^2, Perimeter = 120 m)
    # Section 16: metric_area < 10 AND compactness < 0.05
    sliver_area = 8.0
    sliver_perimeter = 120.0
    compactness = calc(sliver_area, sliver_perimeter)
    assert compactness < 0.05
    assert sliver_area < 10.0

    # Non-positive values return 0.0
    assert calc(0.0, 50.0) == 0.0
    assert calc(100.0, 0.0) == 0.0
    assert calc(-10.0, 10.0) == 0.0


def test_parcel_instantiation_and_relationship():
    layer_id = uuid.uuid4()
    wkt_poly = "MULTIPOLYGON(((0 0, 10 0, 10 10, 0 10, 0 0)))"

    parcel = HarmonizedParcel(
        layer_id=layer_id,
        parcel_identifier="KHASRA-42-A",
        geometry_wgs84=WKTElement(wkt_poly, srid=4326),
        geometry_projected=WKTElement(wkt_poly, srid=32643),
        metric_area_sqm=100.0,
        metric_perimeter_m=40.0,
        polsby_popper_compactness=0.7854,
    )

    assert parcel.parcel_identifier == "KHASRA-42-A"
    assert parcel.layer_id == layer_id
    assert parcel.metric_area_sqm == 100.0
    assert parcel.metric_perimeter_m == 40.0
    assert parcel.is_sliver is False
    assert parcel.is_disputed is False
    assert parcel.dispute_locked is False
    assert parcel.resolution_status == "ORIGINAL"
