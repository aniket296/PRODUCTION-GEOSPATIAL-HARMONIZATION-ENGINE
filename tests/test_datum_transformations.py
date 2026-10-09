"""Comprehensive unit test suite for Geodetic Datum Transformations.

Tests rigorous 3D Cartesian conversions, 7-parameter Helmert (Position Vector and Bursa-Wolf / Coordinate Frame),
Molodensky-Badekas 10-parameter models, Survey of India Kalianpur 1975 to WGS 84 pipelines,
and 2D distortion grid shift interpolations.
"""

from __future__ import annotations

import math
import pytest

from backend.geodetic import (
    DatumPipeline,
    EllipsoidConversion,
    EVEREST_1830_ELLIPSOID,
    EVEREST_1830_TO_WGS84_SOI_7P,
    GRS80_ELLIPSOID,
    GridOutOfBoundsError,
    GridShiftTable,
    HelmertConvention,
    HelmertFitResult,
    HelmertFitter,
    HelmertParameters,
    HelmertTransform,
    KALIANPUR_1975_TO_WGS84_NATIONAL,
    KALIANPUR_1975_ZONE_IIA_REGIONAL,
    TransformationError,
    WGS84_ELLIPSOID,
    get_transformation_pipeline,
    transform_coordinates_batch,
    transform_geojson_geometry,
)


class TestEllipsoidConversion:
    """Tests for geodetic (lon, lat, height) <-> geocentric Cartesian (X, Y, Z) transformations."""

    def test_equator_prime_meridian_wgs84(self):
        # Point at 0°E, 0°N, 0m on WGS 84 should be (a, 0, 0)
        x, y, z = EllipsoidConversion.geodetic_to_cartesian(0.0, 0.0, 0.0, WGS84_ELLIPSOID)
        assert pytest.approx(x, abs=1e-3) == WGS84_ELLIPSOID.semi_major_axis
        assert pytest.approx(y, abs=1e-3) == 0.0
        assert pytest.approx(z, abs=1e-3) == 0.0

    def test_north_pole_wgs84(self):
        # Point at 0°E, 90°N, 0m on WGS 84 should be (0, 0, b)
        x, y, z = EllipsoidConversion.geodetic_to_cartesian(0.0, 90.0, 0.0, WGS84_ELLIPSOID)
        assert pytest.approx(x, abs=1e-3) == 0.0
        assert pytest.approx(y, abs=1e-3) == 0.0
        assert pytest.approx(z, abs=1e-3) == WGS84_ELLIPSOID.semi_minor_axis

    def test_south_pole_everest_1830(self):
        # Point at 0°E, -90°N, 100m on Everest 1830 should be (0, 0, -(b + 100))
        x, y, z = EllipsoidConversion.geodetic_to_cartesian(0.0, -90.0, 100.0, EVEREST_1830_ELLIPSOID)
        assert pytest.approx(x, abs=1e-3) == 0.0
        assert pytest.approx(y, abs=1e-3) == 0.0
        expected_z = -(EVEREST_1830_ELLIPSOID.semi_minor_axis + 100.0)
        assert pytest.approx(z, abs=1e-3) == expected_z

    @pytest.mark.parametrize(
        "lon,lat,h,ellipsoid",
        [
            (77.2090, 28.6139, 216.0, WGS84_ELLIPSOID),  # New Delhi
            (72.8777, 19.0760, 14.0, WGS84_ELLIPSOID),   # Mumbai
            (88.3639, 22.5726, 9.0, EVEREST_1830_ELLIPSOID),  # Kolkata on Everest
            (80.2707, 13.0827, 6.0, EVEREST_1830_ELLIPSOID),  # Chennai on Everest
            (77.5670, 34.1526, 3500.0, WGS84_ELLIPSOID), # High altitude Leh, Ladakh
            (75.0000, 20.0000, -50.0, GRS80_ELLIPSOID),  # Negative ellipsoidal height
        ],
    )
    def test_round_trip_closure_sub_millimetre(self, lon, lat, h, ellipsoid):
        # Convert to 3D Cartesian
        x, y, z = EllipsoidConversion.geodetic_to_cartesian(lon, lat, h, ellipsoid)
        # Convert back to Geodetic
        lon_back, lat_back, h_back = EllipsoidConversion.cartesian_to_geodetic(x, y, z, ellipsoid)

        assert pytest.approx(lon_back, abs=1e-9) == lon
        assert pytest.approx(lat_back, abs=1e-9) == lat
        assert pytest.approx(h_back, abs=1e-4) == h  # sub-millimetre precision


class TestHelmertTransforms:
    """Tests for 7-parameter Helmert, Bursa-Wolf, and Molodensky-Badekas models."""

    def test_pure_translation_shift(self):
        params = HelmertParameters(
            name="Pure Translation",
            dx=100.0,
            dy=-50.0,
            dz=25.0,
            convention=HelmertConvention.POSITION_VECTOR,
        )
        p = (1000.0, 2000.0, 3000.0)
        p_trans = HelmertTransform.transform_cartesian(p, params)
        assert pytest.approx(p_trans[0], abs=1e-6) == 1100.0
        assert pytest.approx(p_trans[1], abs=1e-6) == 1950.0
        assert pytest.approx(p_trans[2], abs=1e-6) == 3025.0

    def test_pure_scale_difference(self):
        params = HelmertParameters(
            name="Pure Scale",
            dx=0.0,
            dy=0.0,
            dz=0.0,
            scale_ppm=10.0,  # 10 ppm = 1 + 1e-5
            convention=HelmertConvention.POSITION_VECTOR,
        )
        p = (1000000.0, 2000000.0, 3000000.0)
        p_trans = HelmertTransform.transform_cartesian(p, params)
        assert pytest.approx(p_trans[0], abs=1e-3) == 1000010.0
        assert pytest.approx(p_trans[1], abs=1e-3) == 2000020.0
        assert pytest.approx(p_trans[2], abs=1e-3) == 3000030.0

    def test_position_vector_vs_coordinate_frame_rotations(self):
        # Position Vector and Coordinate Frame have opposite rotation signs
        p = (1000000.0, 2000000.0, 3000000.0)
        pv_params = HelmertParameters(
            name="PV",
            dx=0.0,
            dy=0.0,
            dz=0.0,
            rz=2.0,  # 2 arcseconds
            convention=HelmertConvention.POSITION_VECTOR,
        )
        cf_params = HelmertParameters(
            name="CF",
            dx=0.0,
            dy=0.0,
            dz=0.0,
            rz=2.0,  # 2 arcseconds
            convention=HelmertConvention.COORDINATE_FRAME,
        )

        p_pv = HelmertTransform.transform_cartesian(p, pv_params)
        p_cf = HelmertTransform.transform_cartesian(p, cf_params)

        # In PV: x' = x - rz*y; in CF: x' = x + rz*y
        delta_pv = p_pv[0] - p[0]
        delta_cf = p_cf[0] - p[0]
        assert pytest.approx(delta_pv, abs=1e-5) == -delta_cf

    def test_molodensky_badekas_centroid_model(self):
        centroid = (1500000.0, 2500000.0, 3500000.0)
        params = HelmertParameters(
            name="Molodensky-Badekas",
            dx=50.0,
            dy=60.0,
            dz=70.0,
            rx=0.5,
            ry=-0.5,
            rz=0.8,
            scale_ppm=2.0,
            x0=centroid[0],
            y0=centroid[1],
            z0=centroid[2],
        )
        assert params.is_molodensky_badekas is True

        # At the exact centroid, rotation and scale produce zero relative offset
        p_trans = HelmertTransform.transform_cartesian(centroid, params)
        assert pytest.approx(p_trans[0], abs=1e-5) == centroid[0] + 50.0
        assert pytest.approx(p_trans[1], abs=1e-5) == centroid[1] + 60.0
        assert pytest.approx(p_trans[2], abs=1e-5) == centroid[2] + 70.0

    def test_forward_and_inverse_round_trip_identity(self):
        params = HelmertParameters(
            name="Complex 7P",
            dx=285.5,
            dy=730.2,
            dz=255.8,
            rx=0.15,
            ry=-0.25,
            rz=0.45,
            scale_ppm=-1.5,
            convention=HelmertConvention.COORDINATE_FRAME,
        )
        p_orig = (1234567.89, 2345678.91, 3456789.12)
        p_fwd = HelmertTransform.transform_cartesian(p_orig, params, inverse=False)
        p_back = HelmertTransform.transform_cartesian(p_fwd, params, inverse=True)

        assert pytest.approx(p_back[0], abs=1e-6) == p_orig[0]
        assert pytest.approx(p_back[1], abs=1e-6) == p_orig[1]
        assert pytest.approx(p_back[2], abs=1e-6) == p_orig[2]


class TestSurveyOfIndiaDatumPipelines:
    """Tests for official Indian cadastral and geodetic datum transitions."""

    def test_kalianpur_1975_to_wgs84_national_mean(self):
        # Delhi Kalianpur 1975 coordinates
        lon_kalianpur = 77.2000
        lat_kalianpur = 28.6000
        h_kalianpur = 200.0

        lon_wgs84, lat_wgs84, h_wgs84 = HelmertTransform.transform_geodetic(
            lon_kalianpur,
            lat_kalianpur,
            h_kalianpur,
            EVEREST_1830_ELLIPSOID,
            WGS84_ELLIPSOID,
            KALIANPUR_1975_TO_WGS84_NATIONAL,
        )

        # Coordinate shifts in India between Kalianpur/Everest and WGS84 are typically:
        # Longitude shift: ~2 to 5 arcseconds
        # Latitude shift: ~1 to 4 arcseconds
        d_lon_sec = abs(lon_wgs84 - lon_kalianpur) * 3600.0
        d_lat_sec = abs(lat_wgs84 - lat_kalianpur) * 3600.0

        assert 0.1 <= d_lon_sec <= 10.0
        assert 0.1 <= d_lat_sec <= 10.0

        # Round-trip transformation check
        lon_inv, lat_inv, h_inv = HelmertTransform.transform_geodetic(
            lon_wgs84,
            lat_wgs84,
            h_wgs84,
            WGS84_ELLIPSOID,
            EVEREST_1830_ELLIPSOID,
            KALIANPUR_1975_TO_WGS84_NATIONAL,
            inverse=True,
        )
        assert pytest.approx(lon_inv, abs=1e-7) == lon_kalianpur
        assert pytest.approx(lat_inv, abs=1e-7) == lat_kalianpur
        assert pytest.approx(h_inv, abs=1e-3) == h_kalianpur

    def test_everest_to_wgs84_soi_7parameter(self):
        # Pune Kalianpur coordinates
        lon_k, lat_k, h_k = 73.8567, 18.5204, 550.0

        lon_w, lat_w, h_w = HelmertTransform.transform_geodetic(
            lon_k,
            lat_k,
            h_k,
            EVEREST_1830_ELLIPSOID,
            WGS84_ELLIPSOID,
            EVEREST_1830_TO_WGS84_SOI_7P,
        )

        assert lon_w != lon_k
        assert lat_w != lat_k
        assert h_w != h_k

        # Inverse transform
        lon_ret, lat_ret, h_ret = HelmertTransform.transform_geodetic(
            lon_w,
            lat_w,
            h_w,
            WGS84_ELLIPSOID,
            EVEREST_1830_ELLIPSOID,
            EVEREST_1830_TO_WGS84_SOI_7P,
            inverse=True,
        )
        assert pytest.approx(lon_ret, abs=1e-7) == lon_k
        assert pytest.approx(lat_ret, abs=1e-7) == lat_k
        assert pytest.approx(h_ret, abs=1e-3) == h_k


class TestGridShiftInterpolation:
    """Tests for 2D distortion grid shift modeling and bilinear interpolation."""

    @pytest.fixture
    def sample_grid(self) -> GridShiftTable:
        # 3x3 node grid covering Lon 76.0 to 78.0, Lat 28.0 to 30.0 (step 1.0 deg)
        # Shifts in arcseconds: (d_lon_sec, d_lat_sec)
        shifts = [
            [(1.0, 2.0), (1.2, 2.1), (1.4, 2.2)],  # Lat 28.0 (row 0)
            [(1.1, 2.2), (1.3, 2.3), (1.5, 2.4)],  # Lat 29.0 (row 1)
            [(1.2, 2.4), (1.4, 2.5), (1.6, 2.6)],  # Lat 30.0 (row 2)
        ]
        return GridShiftTable(
            name="Delhi Regional Cadastral Shift Grid",
            min_lon=76.0,
            min_lat=28.0,
            max_lon=78.0,
            max_lat=30.0,
            step_lon=1.0,
            step_lat=1.0,
            shifts=shifts,
        )

    def test_grid_node_exact_match(self, sample_grid):
        # Query at exact node (Lon 77.0, Lat 29.0 -> row 1, col 1)
        d_lon, d_lat = sample_grid.interpolate_shift(77.0, 29.0)
        assert pytest.approx(d_lon, abs=1e-5) == 1.3
        assert pytest.approx(d_lat, abs=1e-5) == 2.3

    def test_grid_cell_interior_interpolation(self, sample_grid):
        # Query at centroid of cell [76.0, 28.0] to [77.0, 29.0] -> (76.5, 28.5)
        # Corner values: s00=(1.0, 2.0), s10=(1.2, 2.1), s01=(1.1, 2.2), s11=(1.3, 2.3)
        # Mean d_lon = (1.0 + 1.2 + 1.1 + 1.3) / 4 = 1.15
        # Mean d_lat = (2.0 + 2.1 + 2.2 + 2.3) / 4 = 2.15
        d_lon, d_lat = sample_grid.interpolate_shift(76.5, 28.5)
        assert pytest.approx(d_lon, abs=1e-5) == 1.15
        assert pytest.approx(d_lat, abs=1e-5) == 2.15

    def test_grid_boundary_containment(self, sample_grid):
        assert sample_grid.contains(76.5, 28.5) is True
        assert sample_grid.contains(75.9, 28.5) is False
        assert sample_grid.contains(76.5, 30.1) is False

    def test_grid_out_of_bounds_raises(self, sample_grid):
        with pytest.raises(GridOutOfBoundsError):
            sample_grid.interpolate_shift(85.0, 25.0)

    def test_apply_shift_forward_and_inverse(self, sample_grid):
        lon_in, lat_in = 76.5, 28.5
        lon_shifted, lat_shifted = sample_grid.apply_shift(lon_in, lat_in, inverse=False)
        assert lon_shifted > lon_in
        assert lat_shifted > lat_in

        # Inverse shift
        lon_orig, lat_orig = sample_grid.apply_shift(lon_shifted, lat_shifted, inverse=True)
        # Numerical round-trip accuracy within 1e-7 deg
        assert pytest.approx(lon_orig, abs=1e-6) == lon_in
        assert pytest.approx(lat_orig, abs=1e-6) == lat_in


class TestDatumPipelineExecution:
    """Tests for integrated datum harmonization pipelines."""

    def test_full_pipeline_helmert_plus_grid(self):
        shifts = [
            [(0.5, 0.5), (0.6, 0.6)],
            [(0.7, 0.7), (0.8, 0.8)],
        ]
        grid = GridShiftTable(
            name="Local Residual Grid",
            min_lon=76.0,
            min_lat=28.0,
            max_lon=78.0,
            max_lat=30.0,
            step_lon=2.0,
            step_lat=2.0,
            shifts=shifts,
        )

        pipeline = DatumPipeline(
            name="Kalianpur 1975 to WGS 84 with Local Residuals",
            source_ellipsoid=EVEREST_1830_ELLIPSOID,
            target_ellipsoid=WGS84_ELLIPSOID,
            helmert_params=KALIANPUR_1975_TO_WGS84_NATIONAL,
            grid_shift_table=grid,
        )

        lon_in, lat_in, h_in = 77.0, 29.0, 100.0
        lon_out, lat_out, h_out = pipeline.transform(lon_in, lat_in, h_in, inverse=False)

        assert lon_out != lon_in
        assert lat_out != lat_in

        # Inverse pipeline
        lon_rev, lat_rev, h_rev = pipeline.transform(lon_out, lat_out, h_out, inverse=True)
        assert pytest.approx(lon_rev, abs=1e-5) == lon_in
        assert pytest.approx(lat_rev, abs=1e-5) == lat_in
        assert pytest.approx(h_rev, abs=1e-2) == h_in


class TestHelmertFitter:
    """Tests for least-squares 7-parameter Helmert adjustment from GCPs."""

    def test_fit_exact_synthetic_helmert(self):
        # Known ground truth transformation
        true_params = HelmertParameters(
            name="Ground Truth",
            dx=120.0,
            dy=-85.0,
            dz=45.0,
            rx=1.2,
            ry=-0.8,
            rz=2.1,
            scale_ppm=4.5,
            convention=HelmertConvention.POSITION_VECTOR,
        )

        # 6 synthetic source control points around India (approx Cartesian coordinates)
        source_pts = [
            (1250000.0, 5800000.0, 2100000.0),
            (1350000.0, 5700000.0, 2200000.0),
            (1450000.0, 5900000.0, 2000000.0),
            (1150000.0, 5600000.0, 2300000.0),
            (1300000.0, 5750000.0, 2150000.0),
            (1400000.0, 5650000.0, 2250000.0),
        ]

        # Generate target points using true transformation
        target_pts = [
            HelmertTransform.transform_cartesian(sp, true_params)
            for sp in source_pts
        ]

        # Fit parameters
        fit_res = HelmertFitter.fit(
            source_pts,
            target_pts,
            convention=HelmertConvention.POSITION_VECTOR,
        )

        assert isinstance(fit_res, HelmertFitResult)
        assert fit_res.num_points == 6
        assert fit_res.degrees_of_freedom == 11
        assert pytest.approx(fit_res.rmse_3d, abs=1e-3) == 0.0

        p = fit_res.parameters
        assert pytest.approx(p.dx, abs=1e-3) == true_params.dx
        assert pytest.approx(p.dy, abs=1e-3) == true_params.dy
        assert pytest.approx(p.dz, abs=1e-3) == true_params.dz
        assert pytest.approx(p.rx, abs=1e-3) == true_params.rx
        assert pytest.approx(p.ry, abs=1e-3) == true_params.ry
        assert pytest.approx(p.rz, abs=1e-3) == true_params.rz
        assert pytest.approx(p.scale_ppm, abs=1e-3) == true_params.scale_ppm

    def test_fit_molodensky_badekas(self):
        source_pts = [
            (1000000.0, 2000000.0, 3000000.0),
            (1050000.0, 2050000.0, 3050000.0),
            (1100000.0, 2000000.0, 3000000.0),
            (1000000.0, 2100000.0, 3100000.0),
        ]
        target_pts = [
            (p[0] + 50.0, p[1] + 60.0, p[2] + 70.0)
            for p in source_pts
        ]

        fit_res = HelmertFitter.fit(
            source_pts,
            target_pts,
            is_molodensky_badekas=True,
        )
        assert fit_res.parameters.is_molodensky_badekas is True
        assert pytest.approx(fit_res.parameters.dx, abs=1e-3) == 50.0
        assert pytest.approx(fit_res.parameters.dy, abs=1e-3) == 60.0
        assert pytest.approx(fit_res.parameters.dz, abs=1e-3) == 70.0
        assert pytest.approx(fit_res.rmse_3d, abs=1e-3) == 0.0

    def test_fit_validation_minimum_points(self):
        # 2 points is less than minimum 3
        with pytest.raises(TransformationError, match="Minimum 3"):
            HelmertFitter.fit([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)], [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])

    def test_fit_validation_point_mismatch(self):
        with pytest.raises(TransformationError, match="Point count mismatch"):
            HelmertFitter.fit([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)], [(1.0, 2.0, 3.0)])


class TestBatchAndGeoJSONTransform:
    """Tests for vector coordinate batching and GeoJSON geometry tree traversal."""

    @pytest.fixture
    def national_pipeline(self) -> DatumPipeline:
        return DatumPipeline(
            name="Kalianpur to WGS84",
            source_ellipsoid=EVEREST_1830_ELLIPSOID,
            target_ellipsoid=WGS84_ELLIPSOID,
            helmert_params=KALIANPUR_1975_TO_WGS84_NATIONAL,
        )

    def test_transform_coordinates_batch_2d(self, national_pipeline):
        coords_in = [(77.20, 28.61), (72.87, 19.07), (88.36, 22.57)]
        res = transform_coordinates_batch(coords_in, national_pipeline)
        assert len(res) == 3
        assert all(len(pt) == 2 for pt in res)
        # Check non-zero transformation shift
        assert res[0][0] != coords_in[0][0]
        assert res[0][1] != coords_in[0][1]

    def test_transform_coordinates_batch_3d(self, national_pipeline):
        coords_in = [(77.20, 28.61, 216.0), (72.87, 19.07, 14.0)]
        res = transform_coordinates_batch(coords_in, national_pipeline)
        assert len(res) == 2
        assert all(len(pt) == 3 for pt in res)

    def test_transform_geojson_polygon(self, national_pipeline):
        poly_geom = {
            "type": "Polygon",
            "coordinates": [
                [
                    [77.20, 28.60],
                    [77.25, 28.60],
                    [77.25, 28.65],
                    [77.20, 28.65],
                    [77.20, 28.60],
                ]
            ],
        }
        res = transform_geojson_geometry(poly_geom, national_pipeline)
        assert res["type"] == "Polygon"
        ring = res["coordinates"][0]
        assert len(ring) == 5
        # First and last point remain closed ring
        assert ring[0] == ring[4]
        assert ring[0][0] != 77.20

    def test_transform_geojson_geometry_collection(self, national_pipeline):
        geom_coll = {
            "type": "GeometryCollection",
            "geometries": [
                {"type": "Point", "coordinates": [77.20, 28.60]},
                {"type": "LineString", "coordinates": [[77.20, 28.60], [77.25, 28.65]]},
            ],
        }
        res = transform_geojson_geometry(geom_coll, national_pipeline)
        assert res["type"] == "GeometryCollection"
        assert len(res["geometries"]) == 2
        assert res["geometries"][0]["type"] == "Point"
        assert res["geometries"][1]["type"] == "LineString"


class TestPipelineLookup:
    """Tests for automatic pipeline construction from SRIDs."""

    def test_pipeline_lookup_kalianpur_to_wgs84(self):
        pipe = get_transformation_pipeline("EPSG:4240", "EPSG:4326")
        assert pipe is not None
        assert "Everest" in pipe.source_ellipsoid.name
        assert "WGS 84" in pipe.target_ellipsoid.name
        assert pipe.helmert_params is not None

    def test_pipeline_lookup_zone_iia_regional(self):
        pipe = get_transformation_pipeline("EPSG:24379", "EPSG:4326")
        assert pipe is not None
        assert pipe.helmert_params.dx == KALIANPUR_1975_ZONE_IIA_REGIONAL.dx

    def test_pipeline_lookup_same_srid_is_identity(self):
        pipe = get_transformation_pipeline("EPSG:4326", "EPSG:4326")
        assert pipe is not None
        assert pipe.helmert_params is None

