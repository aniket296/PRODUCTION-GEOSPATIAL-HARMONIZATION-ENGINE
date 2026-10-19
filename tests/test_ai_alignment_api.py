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


class TestDeformationAPI:
    """Tests for POST /api/v1/ai/deformation endpoints."""

    def test_deform_polygon_endpoint(self):
        poly = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0]]],
        }
        vectors = [
            {"x": 50.0, "y": 50.0, "dx": 5.0, "dy": 5.0, "weight": 1.0},
        ]
        payload = {
            "parcel_id": "DEFORM-1",
            "geojson": poly,
            "displacement_vectors": vectors,
        }

        resp = client.post("/api/v1/ai/deformation/deform", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["parcel_id"] == "DEFORM-1"
        assert data["deformed_geojson"]["type"] == "Polygon"
        assert data["audit"]["total_vertices_deformed"] > 0
        assert data["audit"]["topology_preserved"] is True
        assert data["audit"]["rms_displacement"] > 0.0

    def test_deform_polygon_clamping(self):
        poly = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [0.0, 50.0], [0.0, 0.0]]],
        }
        # Vector with 100m displacement, exceeds 10m max_displacement
        vectors = [
            {"x": 25.0, "y": 25.0, "dx": 100.0, "dy": 0.0, "weight": 1.0},
        ]
        payload = {
            "parcel_id": "CLAMP-1",
            "geojson": poly,
            "displacement_vectors": vectors,
            "config": {
                "max_displacement_m": 10.0,
            },
        }

        resp = client.post("/api/v1/ai/deformation/deform", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["audit"]["clamped_vertices_count"] > 0
        assert data["audit"]["max_displacement_observed"] <= 10.01

    def test_snap_to_satellite_endpoint(self):
        cad_poly = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0]]],
        }
        sat_poly = {
            "type": "Polygon",
            "coordinates": [[[4.0, 4.0], [104.0, 4.0], [104.0, 104.0], [4.0, 104.0], [4.0, 4.0]]],
        }
        payload = {
            "parcel_id": "SNAP-1",
            "cadastre_geojson": cad_poly,
            "satellite_geojson": sat_poly,
            "num_boundary_samples": 40,
        }

        resp = client.post("/api/v1/ai/deformation/snap", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["parcel_id"] == "SNAP-1"
        assert data["generated_vectors_count"] > 0
        assert data["audit"]["topology_preserved"] is True
        assert data["audit"]["max_displacement_observed"] > 0.0

    def test_deform_cluster_endpoint(self):
        parcel_a = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [0.0, 50.0], [0.0, 0.0]]],
        }
        parcel_b = {
            "type": "Polygon",
            "coordinates": [[[50.0, 0.0], [100.0, 0.0], [100.0, 50.0], [50.0, 50.0], [50.0, 0.0]]],
        }
        vectors = [
            {"x": 25.0, "y": 25.0, "dx": 3.0, "dy": 0.0, "weight": 1.0},
            {"x": 75.0, "y": 25.0, "dx": 3.0, "dy": 0.0, "weight": 1.0},
        ]
        payload = {
            "parcels": [
                {"parcel_id": "CLUSTER-A", "geojson": parcel_a},
                {"parcel_id": "CLUSTER-B", "geojson": parcel_b},
            ],
            "displacement_vectors": vectors,
        }

        resp = client.post("/api/v1/ai/deformation/cluster", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_parcels"] == 2
        assert len(data["results"]) == 2
        assert data["results"][0]["parcel_id"] == "CLUSTER-A"
        assert data["results"][1]["parcel_id"] == "CLUSTER-B"
        assert data["results"][0]["audit"]["topology_preserved"] is True
        assert data["results"][1]["audit"]["topology_preserved"] is True

    def test_deform_invalid_input(self):
        payload = {
            "parcel_id": "BAD",
            "geojson": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "displacement_vectors": [{"x": 0, "y": 0, "dx": 1, "dy": 1}],
        }
        resp = client.post("/api/v1/ai/deformation/deform", json=payload)
        assert resp.status_code == 400


class TestSuperResolutionAPI:
    """Tests for POST /api/v1/ai/super-resolution endpoints."""

    def test_enhance_raster_endpoint(self):
        # 8x8 synthetic patch with step edge
        matrix = [[0.2 if c < 4 else 0.8 for c in range(8)] for _ in range(8)]
        payload = {
            "tile_id": "SENTINEL2-TILE-01",
            "raster_matrix": matrix,
            "config": {
                "scale_factor": 2,
                "sharpen_amount": 1.2,
                "apply_contrast_enhancement": True,
                "original_gsd_m": 10.0,
            },
        }

        resp = client.post("/api/v1/ai/super-resolution/enhance", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["tile_id"] == "SENTINEL2-TILE-01"
        assert data["audit"]["original_shape"] == [8, 8]
        assert data["audit"]["enhanced_shape"] == [16, 16]
        assert data["audit"]["enhanced_gsd_m"] == 5.0
        assert data["audit"]["sharpness_gain_ratio"] > 1.0
        assert len(data["enhanced_matrix"]) == 16
        assert len(data["enhanced_matrix"][0]) == 16

    def test_extract_boundary_mask_endpoint(self):
        matrix = [[0.2 if r + c < 8 else 0.85 for c in range(8)] for r in range(8)]
        payload = {
            "tile_id": "SENTINEL2-MASK-01",
            "raster_matrix": matrix,
            "config": {
                "scale_factor": 2,
                "original_gsd_m": 10.0,
                "edge_threshold": 0.2,
            },
        }

        resp = client.post("/api/v1/ai/super-resolution/boundary-mask", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["tile_id"] == "SENTINEL2-MASK-01"
        assert data["edge_pixel_count"] > 0
        assert data["edge_density_ratio"] > 0.0
        assert data["enhanced_gsd_m"] == 5.0
        assert len(data["boundary_mask"]) == 16
        assert len(data["boundary_mask"][0]) == 16


class TestVectorizationAPI:
    """Tests for POST /api/v1/ai/vectorization/vectorize endpoint."""

    def test_vectorize_foreground_mask_endpoint(self):
        # 20x20 mask with a 10x10 parcel at [5:15, 5:15]
        mask = [[(5 <= r < 15 and 5 <= c < 15) for c in range(20)] for r in range(20)]
        payload = {
            "tile_id": "VEC-TILE-01",
            "binary_mask": mask,
            "mask_type": "FOREGROUND",
            "config": {
                "origin_x": 500000.0,
                "origin_y": 2800000.0,
                "gsd_m": 2.0,
                "simplify_tolerance_m": 0.5,
                "min_area_sqm": 50.0,
            },
        }

        resp = client.post("/api/v1/ai/vectorization/vectorize", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["tile_id"] == "VEC-TILE-01"
        assert data["audit"]["total_parcels_extracted"] == 1
        assert data["audit"]["all_geometries_valid"] is True
        assert pytest.approx(400.0, rel=0.02) == data["parcels"][0]["area_sqm"]
        assert data["parcels"][0]["geojson"]["type"] == "Polygon"




class TestChangeDetectionAPI:
    """Tests for POST /api/v1/ai/change-detection endpoints."""

    @staticmethod
    def _raster_pair():
        # 32x32x3 (Red, NIR, SWIR): vegetated baseline; built-up block at rows 10:26, cols 12:28 in T2
        t1 = [[[0.10, 0.70, 0.15] for _ in range(32)] for _ in range(32)]
        t2 = [[list(px) for px in row] for row in t1]
        for r in range(10, 26):
            for c in range(12, 28):
                t2[r][c] = [0.45, 0.20, 0.55]
        return t1, t2

    def test_analyze_change_detection_flags_encroachment(self):
        t1, t2 = self._raster_pair()
        payload = {
            "parcel_id": "KHASRA-TEMPORAL-API-01",
            "cadastre_geojson": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [40.0, 0.0], [40.0, 64.0], [0.0, 64.0], [0.0, 0.0]]],
            },
            "t1_raster": t1,
            "t2_raster": t2,
            "config": {
                "origin_x": 0.0,
                "origin_y": 64.0,
                "gsd_m": 2.0,
                "change_magnitude_threshold": 0.25,
                "buffer_tolerance_m": 0.0,
                "min_encroachment_sqm": 20.0,
            },
        }

        resp = client.post("/api/v1/ai/change-detection/analyze", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["parcel_id"] == "KHASRA-TEMPORAL-API-01"
        assert data["temporal_change"]["changed_pixel_count"] == 256
        assert data["encroachment"]["has_encroachment"] is True
        assert data["encroachment"]["severity"] == "CRITICAL"
        assert pytest.approx(512.0, rel=0.05) == data["encroachment"]["encroached_area_sqm"]
        assert pytest.approx(16.0, rel=0.05) == data["encroachment"]["max_lateral_intrusion_m"]
        assert len(data["new_footprints"]) == 1

    def test_analyze_change_detection_raster_shape_mismatch(self):
        t1, t2 = self._raster_pair()
        payload = {
            "parcel_id": "BAD-RASTER",
            "cadastre_geojson": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
            },
            "t1_raster": t1,
            "t2_raster": t2[:16],
        }
        resp = client.post("/api/v1/ai/change-detection/analyze", json=payload)
        assert resp.status_code == 400

    def test_evaluate_encroachment_with_public_corridor(self):
        payload = {
            "parcel_id": "KHASRA-ROW-01",
            "cadastre_geojson": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0]]],
            },
            "occupied_geojson": {
                "type": "Polygon",
                "coordinates": [[
                    [0.0, 0.0], [100.0, 0.0], [100.0, 20.0], [112.0, 20.0],
                    [112.0, 70.0], [100.0, 70.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0],
                ]],
            },
            "public_corridors": {
                "NH-ROW-01": {
                    "type": "Polygon",
                    "coordinates": [[[100.0, 0.0], [120.0, 0.0], [120.0, 100.0], [100.0, 100.0], [100.0, 0.0]]],
                }
            },
            "buffer_tolerance_m": 0.0,
            "min_encroachment_sqm": 10.0,
        }

        resp = client.post("/api/v1/ai/change-detection/encroachment", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_encroachment"] is True
        assert pytest.approx(600.0, rel=0.01) == data["encroached_area_sqm"]
        assert pytest.approx(600.0, rel=0.01) == data["public_corridor_intrusion_sqm"]
        assert data["affected_corridor_ids"] == ["NH-ROW-01"]
        assert data["severity"] in ("MODERATE", "CRITICAL")

    def test_evaluate_encroachment_invalid_geometry(self):
        payload = {
            "parcel_id": "BAD-GEOM",
            "cadastre_geojson": {"type": "Point", "coordinates": [0.0, 0.0]},
            "occupied_geojson": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
            },
        }
        resp = client.post("/api/v1/ai/change-detection/encroachment", json=payload)
        assert resp.status_code == 400
