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
