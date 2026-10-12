"""Integration tests for AI boundary alignment scoring REST API endpoints."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.api.app import app

client = TestClient(app)


class TestBoundaryAlignmentAPI:
    """Tests for POST /api/v1/ai/alignment/score and /api/v1/ai/alignment/batch."""

    def test_score_boundary_alignment_perfect(self):
        cad_geojson = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0]]],
        }
        sat_geojson = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0]]],
        }

        payload = {
            "parcel_id": "KHASRA-PERFECT-1",
            "cadastre_geojson": cad_geojson,
            "satellite_geojson": sat_geojson,
            "tolerance_buffer_m": 2.0,
        }

        resp = client.post("/api/v1/ai/alignment/score", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["parcel_id"] == "KHASRA-PERFECT-1"
        assert data["iou"] >= 0.99
        assert data["hausdorff_distance"] <= 0.05
        assert data["boundary_f1"] >= 0.99
        assert data["category"] == "EXCELLENT"
        assert data["overall_score"] >= 95.0

    def test_score_boundary_alignment_shifted_with_hint(self):
        cad_geojson = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0]]],
        }
        # Shifted by +6m on X, +8m on Y
        sat_geojson = {
            "type": "Polygon",
            "coordinates": [[[6.0, 8.0], [106.0, 8.0], [106.0, 108.0], [6.0, 108.0], [6.0, 8.0]]],
        }

        payload = {
            "parcel_id": "KHASRA-SHIFTED-1",
            "cadastre_geojson": cad_geojson,
            "satellite_geojson": sat_geojson,
            "tolerance_buffer_m": 3.0,
        }

        resp = client.post("/api/v1/ai/alignment/score", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["parcel_id"] == "KHASRA-SHIFTED-1"
        assert data["category"] in ("ACCEPTABLE", "POOR")
        assert data["centroid_displacement_x"] == 6.0
        assert data["centroid_displacement_y"] == 8.0
        assert data["centroid_displacement_distance"] == 10.0

        # Verify alignment hint suggested compensation
        hint = data["alignment_hint"]
        assert hint is not None
        assert hint["suggested_dx"] == -6.0
        assert hint["suggested_dy"] == -8.0

    def test_score_boundary_alignment_invalid_geometry(self):
        payload = {
            "parcel_id": "BAD-GEOM",
            "cadastre_geojson": {"type": "InvalidType"},
            "satellite_geojson": {"type": "Polygon", "coordinates": []},
        }
        resp = client.post("/api/v1/ai/alignment/score", json=payload)
        assert resp.status_code == 400

    def test_score_boundary_alignment_batch(self):
        poly = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [0.0, 50.0], [0.0, 0.0]]],
        }
        poly_shifted = {
            "type": "Polygon",
            "coordinates": [[[2.0, 2.0], [52.0, 2.0], [52.0, 52.0], [2.0, 52.0], [2.0, 2.0]]],
        }

        batch_payload = {
            "items": [
                {
                    "parcel_id": "BATCH-1",
                    "cadastre_geojson": poly,
                    "satellite_geojson": poly,
                    "tolerance_buffer_m": 2.0,
                },
                {
                    "parcel_id": "BATCH-2",
                    "cadastre_geojson": poly,
                    "satellite_geojson": poly_shifted,
                    "tolerance_buffer_m": 2.0,
                },
            ]
        }

        resp = client.post("/api/v1/ai/alignment/batch", json=batch_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_evaluated"] == 2
        assert data["excellent_count"] >= 1
        assert len(data["results"]) == 2
        assert data["average_score"] > 0.0
