"""Integration tests for Geodetic Coordinate Reference System API endpoints."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.api.app import app

client = TestClient(app)


class TestGeodeticCatalogEndpoints:
    """Tests for listing and searching CRS records via REST API."""

    def test_list_crs_returns_200_and_items(self):
        resp = client.get("/api/v1/geodetic/crs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 10
        first = data[0]
        assert "srid" in first
        assert "name" in first
        assert "proj_type" in first

    def test_search_crs_by_query_string(self):
        resp = client.get("/api/v1/geodetic/crs?q=Kalianpur")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        assert any("Kalianpur" in item["name"] for item in data)

    def test_filter_crs_by_proj_type(self):
        resp = client.get("/api/v1/geodetic/crs?proj_type=GEOGRAPHIC_2D")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        assert all(item["proj_type"] == "GEOGRAPHIC_2D" for item in data)

    def test_get_crs_detail_wgs84(self):
        resp = client.get("/api/v1/geodetic/crs/EPSG:4326")
        assert resp.status_code == 200
        data = resp.json()
        assert data["srid"] == "EPSG:4326"
        assert data["epsg_code"] == 4326
        assert data["ellipsoid"]["name"] == "WGS 84"
        assert "bounds" in data

    def test_get_crs_detail_not_found(self):
        resp = client.get("/api/v1/geodetic/crs/EPSG:999999999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestGeodeticValidationEndpoint:
    """Tests for coordinate and bounding box domain validation endpoint."""

    def test_validate_valid_point(self):
        payload = {
            "identifier": "EPSG:32643",
            "coordinates": [75.0, 25.0],  # Within UTM 43N
        }
        resp = client.post("/api/v1/geodetic/crs/validate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_valid_syntax"] is True
        assert data["in_bounds"] is True
        assert data["resolved_srid"] == "EPSG:32643"

    def test_validate_out_of_bounds_point(self):
        payload = {
            "identifier": "EPSG:32643",
            "coordinates": [95.0, 25.0],  # Northeast India, outside UTM 43N
        }
        resp = client.post("/api/v1/geodetic/crs/validate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_valid_syntax"] is True
        assert data["in_bounds"] is False
        assert data["error_detail"] is not None

    def test_validate_unrecognized_crs(self):
        payload = {
            "identifier": "UNKNOWN_CRS_123",
            "coordinates": [75.0, 25.0],
        }
        resp = client.post("/api/v1/geodetic/crs/validate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_valid_syntax"] is False
        assert data["in_bounds"] is False


class TestGeodeticSuggestionEndpoint:
    """Tests for automated zone recommendation endpoint."""

    def test_suggest_for_delhi_coordinates(self):
        payload = {
            "longitude": 77.2090,
            "latitude": 28.6139,
            "state": "Delhi",
        }
        resp = client.post("/api/v1/geodetic/crs/suggest", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["suggested_utm_epsg"] == 32643
        assert data["suggested_utm_srid"] == "EPSG:32643"
        assert data["suggested_soi_epsg"] is not None
        assert data["state_name"] == "Delhi"

    def test_suggest_with_toposheet(self):
        payload = {
            "longitude": 0.0,
            "latitude": 0.0,
            "toposheet": "53H",  # Delhi degree sheet
        }
        resp = client.post("/api/v1/geodetic/crs/suggest", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["suggested_utm_epsg"] == 32643

    def test_suggest_invalid_coordinates(self):
        payload = {
            "longitude": 250.0,
            "latitude": 40.0,
        }
        resp = client.post("/api/v1/geodetic/crs/suggest", json=payload)
        assert resp.status_code == 400


class TestLocalGridRegistrationEndpoint:
    """Tests for registering custom village cadastral survey frame."""

    def test_register_local_grid(self):
        payload = {
            "grid_id": "VILLAGE_KHARADI_01",
            "name": "Kharadi Village Cadastral Frame",
            "village_name": "Kharadi",
            "district": "Pune",
            "state": "Maharashtra",
            "origin_lon": 73.9450,
            "origin_lat": 18.5510,
            "false_easting": 5000.0,
            "false_northing": 5000.0,
            "scale_factor": 1.0,
            "azimuth_rotation_deg": 0.0,
            "extent_radius_km": 20.0,
        }
        resp = client.post("/api/v1/geodetic/crs/local-grid", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["srid"] == "LOCAL:VILLAGE_KHARADI_01"
        assert data["proj_type"] == "LOCAL"
        assert "+proj=tmerc" in data["proj4"]


class TestToposheetParsingEndpoint:
    """Tests for Survey of India toposheet endpoint."""

    def test_parse_toposheet_53h(self):
        resp = client.get("/api/v1/geodetic/toposheet/53H")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sheet_id"] == "53H"
        assert data["scale"] == "1:250,000"
        assert data["bounds"] == [77.0, 28.0, 78.0, 29.0]
        assert data["suggested_utm_epsg"] == 32643

    def test_parse_toposheet_invalid(self):
        resp = client.get("/api/v1/geodetic/toposheet/INVALID_XYZ")
        assert resp.status_code == 400


class TestDatumTransformationEndpoints:
    """Tests for geodetic transformation API endpoints."""

    def test_transform_point_endpoint(self):
        payload = {
            "source_srid": "EPSG:4240",  # Indian 1975
            "target_srid": "EPSG:4326",  # WGS 84
            "coordinates": [77.2000, 28.6000, 200.0],
        }
        resp = client.post("/api/v1/geodetic/transform/point", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_srid"] == "EPSG:4240"
        assert data["target_srid"] == "EPSG:4326"
        coords = data["transformed_coordinates"]
        assert len(coords) == 3
        assert coords[0] != 77.2000
        assert coords[1] != 28.6000

    def test_transform_batch_endpoint(self):
        payload = {
            "source_srid": "EPSG:4240",
            "target_srid": "EPSG:4326",
            "coordinates": [
                [77.20, 28.60],
                [72.87, 19.07],
            ],
        }
        resp = client.post("/api/v1/geodetic/transform/batch", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert len(data["transformed_coordinates"]) == 2

    def test_fit_helmert_endpoint(self):
        # 4 GCP pairs with pure translation (dx=100, dy=200, dz=300)
        source_pts = [
            [1000000.0, 2000000.0, 3000000.0],
            [1100000.0, 2050000.0, 3050000.0],
            [1200000.0, 2100000.0, 3100000.0],
            [1050000.0, 2020000.0, 3020000.0],
        ]
        gcps = [
            {
                "source_xyz": sp,
                "target_xyz": [sp[0] + 100.0, sp[1] + 200.0, sp[2] + 300.0],
            }
            for sp in source_pts
        ]
        payload = {
            "gcps": gcps,
            "convention": "POSITION_VECTOR",
        }
        resp = client.post("/api/v1/geodetic/transform/fit-helmert", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["num_points"] == 4
        assert pytest.approx(data["rmse_3d"], abs=1e-2) == 0.0
        p = data["parameters"]
        assert pytest.approx(p["dx"], abs=1e-2) == 100.0
        assert pytest.approx(p["dy"], abs=1e-2) == 200.0
        assert pytest.approx(p["dz"], abs=1e-2) == 300.0


class TestBoundaryNormalizationAndTopologyEndpoints:
    """Tests for boundary normalization, planar topology validation, and edge snapping endpoints."""

    def test_normalize_geometry_endpoint_success(self):
        # Clockwise polygon with duplicate vertex and unclosed ring
        payload = {
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [0.0, 0.0],
                        [0.0, 10.0],
                        [10.0, 10.0],
                        [10.0, 10.0],  # Duplicate
                        [10.0, 0.0],
                        # Not closed
                    ]
                ],
            },
            "config": {
                "snap_precision_m": 0.001,
                "orient_ccw": True,
            },
        }
        resp = client.post("/api/v1/geodetic/normalize/geometry", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        norm_geom = data["normalized_geometry"]
        assert norm_geom["type"] == "Polygon"
        # Must be closed
        coords = norm_geom["coordinates"][0]
        assert coords[0] == coords[-1]
        audit = data["audit"]
        assert audit["is_valid_polygon"] is True
        assert audit["original_area_sqm"] == pytest.approx(100.0)
        assert audit["normalized_area_sqm"] == pytest.approx(100.0)

    def test_normalize_geometry_endpoint_bowtie_repair(self):
        # Self-intersecting figure-8 bow-tie
        payload = {
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[0.0, 0.0], [10.0, 10.0], [10.0, 0.0], [0.0, 10.0], [0.0, 0.0]]
                ],
            }
        }
        resp = client.post("/api/v1/geodetic/normalize/geometry", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["audit"]["is_valid_polygon"] is True
        assert "INVALID_GEOMETRY" in data["audit"]["repaired_defects"]

    def test_normalize_geometry_endpoint_invalid_payload(self):
        payload = {
            "geometry": {
                "type": "InvalidType",
                "coordinates": [],
            }
        }
        resp = client.post("/api/v1/geodetic/normalize/geometry", json=payload)
        assert resp.status_code in (400, 422)

    def test_validate_topology_batch_endpoint_clean(self):
        payload = {
            "parcels": [
                {
                    "parcel_identifier": "P1",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [40, 0], [40, 50], [0, 50], [0, 0]]],
                    },
                },
                {
                    "parcel_identifier": "P2",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[40, 0], [90, 0], [90, 50], [40, 50], [40, 0]]],
                    },
                },
            ]
        }
        resp = client.post("/api/v1/geodetic/topology/validate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        rep = data["report"]
        assert rep["total_parcels"] == 2
        assert rep["valid_parcels_count"] == 2
        assert rep["is_planar_graph_valid"] is True
        assert len(rep["anomalies"]) == 0

    def test_validate_topology_batch_endpoint_with_overlap(self):
        # 0.05m overlap * 50m = 2.5 sqm
        payload = {
            "parcels": [
                {
                    "parcel_identifier": "P1",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [40.05, 0], [40.05, 50], [0, 50], [0, 0]]],
                    },
                },
                {
                    "parcel_identifier": "P2",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[40, 0], [90, 0], [90, 50], [40, 50], [40, 0]]],
                    },
                },
            ]
        }
        resp = client.post("/api/v1/geodetic/topology/validate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        rep = data["report"]
        assert rep["is_planar_graph_valid"] is False
        assert rep["total_overlap_area_sqm"] == pytest.approx(2.5, abs=1e-3)
        assert rep["auto_resolvable_count"] == 1
        assert len(rep["anomalies"]) == 1
        anom = rep["anomalies"][0]
        assert anom["defect_type"] == "OVERLAP"
        assert anom["severity"] == "AUTO_RESOLVABLE"

    def test_snap_shared_boundaries_endpoint(self):
        payload = {
            "parcels": [
                {
                    "parcel_identifier": "P1",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [40, 0], [40, 50], [0, 50], [0, 0]]],
                    },
                },
                {
                    "parcel_identifier": "P2",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[40.02, 0], [90, 0], [90, 50], [40.02, 50], [40.02, 0]]],
                    },
                },
            ],
            "tolerance_m": 0.05,
        }
        resp = client.post("/api/v1/geodetic/topology/snap", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["count"] == 2
        aligned = data["aligned_parcels"]
        assert len(aligned) == 2


