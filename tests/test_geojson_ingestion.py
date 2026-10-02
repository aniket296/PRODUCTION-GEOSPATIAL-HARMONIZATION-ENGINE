"""Tests for GeoJSON RFC 7946 validation, CRS urn extraction, and coordinate precision rounding."""

import json
from pathlib import Path
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon

from backend.ingestion.geojson_reader import (
    GeoJSONCorruptFileError,
    GeoJSONEmptyFileError,
    GeoJSONFeatureRecord,
    GeoJSONIngestionReader,
    GeoJSONIngestionResult,
    GeoJSONValidationError,
    enforce_rfc7946_winding_order,
    extract_crs_from_geojson,
    normalize_cadastral_properties,
    remove_duplicate_consecutive_vertices,
    round_coordinates,
)
from fixtures.cadastral_fixtures import (
    EVEREST_1830_CRS,
    WGS84_GEO_CRS,
    export_fixtures_to_geojson,
    generate_clean_cadastral_fixtures,
    save_fixtures_geojson,
)


def test_extract_crs_from_geojson():
    # Default RFC 7946 without crs member
    assert extract_crs_from_geojson({}) == "EPSG:4326"
    assert extract_crs_from_geojson({"type": "FeatureCollection"}) == "EPSG:4326"

    # OGC standard CRS84
    crs84_doc = {"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}}
    assert extract_crs_from_geojson(crs84_doc) == "EPSG:4326"

    # Everest 1830 URN
    everest_urn_doc = {"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::24378"}}}
    assert extract_crs_from_geojson(everest_urn_doc) == "EPSG:24378"

    # UTM Zone 43N URN
    utm_doc = {"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32643"}}}
    assert extract_crs_from_geojson(utm_doc) == "EPSG:32643"

    # Direct short EPSG name
    short_doc = {"crs": {"type": "name", "properties": {"name": "EPSG:24378"}}}
    assert extract_crs_from_geojson(short_doc) == "EPSG:24378"


def test_round_coordinates_precision():
    # Test geographic coordinate rounding (7 decimals, ~1.1cm precision)
    poly_geo = Polygon([
        (76.1234567891, 28.9876543219),
        (76.1234568891, 28.9876543219),
        (76.1234568891, 28.9876544219),
        (76.1234567891, 28.9876544219),
        (76.1234567891, 28.9876543219),
    ])
    rounded_geo = round_coordinates(poly_geo, precision=7)
    x0, y0 = rounded_geo.exterior.coords[0]
    assert x0 == 76.1234568
    assert y0 == 28.9876543

    # Test projected coordinate rounding (3 decimals, 1mm precision)
    poly_proj = Polygon([
        (3600000.12345, 500000.67891),
        (3600050.12345, 500000.67891),
        (3600050.12345, 500050.67891),
        (3600000.12345, 500050.67891),
        (3600000.12345, 500000.67891),
    ])
    rounded_proj = round_coordinates(poly_proj, precision=3)
    px0, py0 = rounded_proj.exterior.coords[0]
    assert px0 == 3600000.123
    assert py0 == 500000.679


def test_remove_duplicate_consecutive_vertices():
    # Polygon with consecutive duplicate vertices: [(0,0), (0,0), (10,0), (10,10), (10,10), (0,10), (0,0)]
    poly_with_dupes = Polygon([
        (0.0, 0.0),
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 10.0),
        (10.0, 10.0),
        (0.0, 10.0),
        (0.0, 0.0),
    ])
    assert len(poly_with_dupes.exterior.coords) == 7

    cleaned = remove_duplicate_consecutive_vertices(poly_with_dupes)
    assert len(cleaned.exterior.coords) == 5
    assert cleaned.area == 100.0


def test_enforce_rfc7946_winding_order():
    # CW polygon exterior ring: [(0,0), (0,1), (1,1), (1,0), (0,0)]
    cw_poly = Polygon([(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)])
    assert not cw_poly.exterior.is_ccw  # Clockwise

    ccw_poly, corrected = enforce_rfc7946_winding_order(cw_poly)
    assert corrected is True
    assert ccw_poly.exterior.is_ccw  # Enforced counterclockwise per RFC 7946
    assert ccw_poly.is_valid is True
    assert ccw_poly.area == 1.0


def test_read_clean_cadastral_fixtures_geojson():
    parcels = generate_clean_cadastral_fixtures()
    fc_dict = export_fixtures_to_geojson(parcels, target_crs=WGS84_GEO_CRS)

    result = GeoJSONIngestionReader.read_from_dict(fc_dict)

    assert isinstance(result, GeoJSONIngestionResult)
    assert result.detected_crs == "EPSG:4326"
    assert result.total_count == 10
    assert result.valid_count == 10
    assert result.invalid_count == 0

    assert len(result.features) == 10
    for feat in result.features:
        assert isinstance(feat, GeoJSONFeatureRecord)
        assert feat.feature_id.startswith("KHASRA-")
        assert isinstance(feat.geometry, Polygon)
        assert feat.geometry.exterior.is_ccw is True
        assert feat.is_valid is True
        assert "owner_name" in feat.properties
        assert "khasra_number" in feat.properties
        assert feat.area > 0

    assert result.bounds is not None
    minx, miny, maxx, maxy = result.bounds
    assert 68.0 <= minx <= 90.0
    assert 10.0 <= miny <= 35.0


def test_read_projected_cadastral_fixtures_geojson():
    parcels = generate_clean_cadastral_fixtures()
    fc_dict = export_fixtures_to_geojson(parcels, target_crs=EVEREST_1830_CRS)

    result = GeoJSONIngestionReader.read_from_dict(fc_dict)

    assert result.detected_crs == "EPSG:24378"
    assert result.total_count == 10

    # Ensure projected coordinates rounded to millimeter (3 decimals)
    for feat in result.features:
        for x, y in feat.geometry.exterior.coords:
            assert round(x, 3) == x
            assert round(y, 3) == y

    minx, miny, maxx, maxy = result.bounds
    assert minx == 3600000.0
    assert miny == 500000.0


def test_read_from_bytes_and_file(tmp_path: Path):
    parcels = generate_clean_cadastral_fixtures()
    fc_dict = export_fixtures_to_geojson(parcels, target_crs=WGS84_GEO_CRS)
    raw_json_str = json.dumps(fc_dict)

    # Test read_from_text
    result_text = GeoJSONIngestionReader.read_from_text(raw_json_str)
    assert result_text.total_count == 10

    # Test read_from_bytes
    result_bytes = GeoJSONIngestionReader.read_from_bytes(raw_json_str.encode("utf-8"))
    assert result_bytes.total_count == 10

    # Test read_from_file
    target_file = tmp_path / "village_records.geojson"
    save_fixtures_geojson(target_file, target_crs=WGS84_GEO_CRS)
    result_file = GeoJSONIngestionReader.read_from_file(target_file)
    assert result_file.total_count == 10
    assert result_file.valid_count == 10


def test_iter_features_from_file(tmp_path: Path):
    target_file = tmp_path / "stream_records.geojson"
    save_fixtures_geojson(target_file, target_crs=WGS84_GEO_CRS)

    streamed_records = list(GeoJSONIngestionReader.iter_features_from_file(target_file))
    assert len(streamed_records) == 10
    for r in streamed_records:
        assert isinstance(r, GeoJSONFeatureRecord)
        assert r.is_valid is True


def test_explode_multipolygons():
    multi_poly_doc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "PARCEL-COMPLEX",
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
                        [[[20.0, 0.0], [30.0, 0.0], [30.0, 10.0], [20.0, 10.0], [20.0, 0.0]]],
                    ],
                },
                "properties": {"khasra_no": "505"},
            }
        ],
    }

    # With explode_multipolygons = True
    exploded = GeoJSONIngestionReader.read_from_dict(multi_poly_doc, explode_multipolygons=True)
    assert exploded.total_count == 2
    assert exploded.features[0].feature_id == "PARCEL-COMPLEX_part0"
    assert exploded.features[1].feature_id == "PARCEL-COMPLEX_part1"
    assert isinstance(exploded.features[0].geometry, Polygon)

    # With explode_multipolygons = False
    unexploded = GeoJSONIngestionReader.read_from_dict(multi_poly_doc, explode_multipolygons=False)
    assert unexploded.total_count == 1
    assert isinstance(unexploded.features[0].geometry, MultiPolygon)


def test_normalize_cadastral_properties():
    raw_props = {
        "survey_no": "123/A",
        "khatadar": "Ramesh Chandra",
        "landuse": "Agricultural",
        "village_code": "09876",
    }
    norm = normalize_cadastral_properties(raw_props)
    assert norm["khasra_number"] == "123/A"
    assert norm["owner_name"] == "Ramesh Chandra"
    assert norm["land_use"] == "Agricultural"
    assert norm["village_code"] == "09876"


def test_antimeridian_and_out_of_bounds_flags():
    # Feature spanning antimeridian (from lon -179 to +179)
    antimeridian_doc = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-179.0, 0.0], [179.0, 0.0], [179.0, 10.0], [-179.0, 10.0], [-179.0, 0.0]]],
        },
        "properties": {},
    }
    res = GeoJSONIngestionReader.read_from_dict(antimeridian_doc, target_crs="EPSG:4326")
    assert res.features[0].properties.get("antimeridian_crossing") is True


def test_naked_geometry_and_single_feature():
    single_feat = {
        "type": "Feature",
        "id": "PARCEL-001",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
        },
        "properties": {"khasra": "1"},
    }
    res_feat = GeoJSONIngestionReader.read_from_dict(single_feat)
    assert res_feat.total_count == 1
    assert res_feat.features[0].feature_id == "PARCEL-001"
    assert res_feat.features[0].properties["khasra_number"] == "1"

    naked_geom = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0], [0.0, 0.0]]],
    }
    res_geom = GeoJSONIngestionReader.read_from_dict(naked_geom)
    assert res_geom.total_count == 1
    assert res_geom.features[0].geometry.area == 400.0


def test_error_handling_malformed_and_empty_geojson(tmp_path: Path):
    with pytest.raises(GeoJSONValidationError, match="Expected GeoJSON root dict"):
        GeoJSONIngestionReader.read_from_dict(["not", "a", "dict"])  # type: ignore

    with pytest.raises(GeoJSONValidationError, match="Missing required 'type'"):
        GeoJSONIngestionReader.read_from_dict({"data": []})

    with pytest.raises(GeoJSONEmptyFileError, match="0 features"):
        GeoJSONIngestionReader.read_from_dict({"type": "FeatureCollection", "features": []})

    with pytest.raises(GeoJSONCorruptFileError, match="empty"):
        GeoJSONIngestionReader.read_from_text("")

    with pytest.raises(GeoJSONCorruptFileError, match="Malformed JSON"):
        GeoJSONIngestionReader.read_from_text("{invalid_json_payload...")

    with pytest.raises(FileNotFoundError):
        GeoJSONIngestionReader.read_from_file(tmp_path / "non_existent.geojson")
