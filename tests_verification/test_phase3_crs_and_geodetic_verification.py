"""Phase 3 & 4 Verification: CRS handling, reprojection invariants, and Helmert 3D accuracy."""

from __future__ import annotations

import json
import math
import pyproj
import pytest
from shapely.geometry import Polygon

from backend.geodetic.registry import EVEREST_1830_ELLIPSOID, WGS84_ELLIPSOID
from backend.geodetic.transformations import (
    EVEREST_1830_TO_WGS84_SOI_7P,
    HelmertConvention,
    HelmertParameters,
    HelmertTransform,
)
from backend.ingestion.geojson_reader import GeoJSONIngestionReader
from backend.topology.recorder import project_metric_geometry_to_wgs84


class TestCRSAndGeodeticVerification:
    """Executes verification checks for geodetic transformations and ingestion CRS invariants."""

    def test_finding_f01_geojson_target_crs_reprojects_utm_to_wgs84(self) -> None:
        """FINDING F-01 REMEDIATED (Verified by execution):
        Passing target_crs='EPSG:4326' to GeoJSONIngestionReader extracts source_crs='EPSG:32643'
        and reprojects the UTM Zone 43N metre coordinates into valid WGS 84 degrees.
        """
        utm_geojson = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:32643"}},
            "features": [
                {
                    "type": "Feature",
                    "id": "P-UTM-1",
                    "properties": {"khasra": "101"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [500000.0, 3000000.0],
                                [500100.0, 3000000.0],
                                [500100.0, 3000100.0],
                                [500000.0, 3000100.0],
                                [500000.0, 3000000.0],
                            ]
                        ],
                    },
                }
            ],
        }

        res = GeoJSONIngestionReader.read_from_bytes(
            json.dumps(utm_geojson).encode("utf-8"),
            target_crs="EPSG:4326",
        )

        assert res.detected_crs == "EPSG:4326"
        minx, miny, maxx, maxy = res.bounds  # type: ignore[misc]
        assert 74.0 <= minx <= 76.0
        assert 26.5 <= miny <= 28.0
        assert res.features[0].properties.get("out_of_bounds_wgs84") is not True

    def test_finding_f05_helmert_to_inverse_matches_analytical_inverse(self) -> None:
        """FINDING F-05 REMEDIATED (Verified by execution):
        HelmertParameters.to_inverse() transforms the translation vector by -(I + R)^-1 / scale
        so forward-transforming with params.to_inverse() matches the analytical inverse to sub-millimetre precision.
        """
        params = HelmertParameters(
            dx=295.0,
            dy=736.0,
            dz=257.0,
            rx=5.0,
            ry=-3.0,
            rz=12.0,
            scale_ppm=8.5,
            convention=HelmertConvention.POSITION_VECTOR,
            x0=1000000.0,
            y0=5000000.0,
            z0=3000000.0,
        )
        pt = (1150000.0, 5600000.0, 3100000.0)
        fwd = HelmertTransform.transform_cartesian(pt, params, inverse=False)

        exact_inv = HelmertTransform.transform_cartesian(fwd, params, inverse=True)
        exact_err_m = math.dist(pt, exact_inv)
        assert exact_err_m < 1e-7

        param_inv = HelmertTransform.transform_cartesian(fwd, params.to_inverse(), inverse=False)
        param_err_m = math.dist(pt, param_inv)
        assert param_err_m < 1e-4  # < 0.1 mm sub-millimetre precision

    def test_utm43n_to_wgs84_matches_independent_pyproj_reference(self) -> None:
        """Cross-checks project_metric_geometry_to_wgs84 against raw pyproj.Transformer."""
        utm_poly = Polygon(
            [
                (720000.0, 3170000.0),
                (720150.0, 3170000.0),
                (720150.0, 3170150.0),
                (720000.0, 3170150.0),
                (720000.0, 3170000.0),
            ]
        )
        projected_wgs84 = project_metric_geometry_to_wgs84(utm_poly, source_srid=32643)

        ref_transformer = pyproj.Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)
        for (x_utm, y_utm), (lon_actual, lat_actual) in zip(
            utm_poly.exterior.coords, projected_wgs84.exterior.coords
        ):
            lon_ref, lat_ref = ref_transformer.transform(x_utm, y_utm)
            assert lon_actual == pytest.approx(lon_ref, abs=1e-12)
            assert lat_actual == pytest.approx(lat_ref, abs=1e-12)

    def test_everest_1830_to_wgs84_roundtrip_with_analytical_inverse(self) -> None:
        """Verifies Everest 1830 <-> WGS 84 geodetic round-trip with analytical inverse."""
        lon0, lat0, h0 = 77.2090, 28.6139, 216.0
        lon_wgs, lat_wgs, h_wgs = HelmertTransform.transform_geodetic(
            lon0,
            lat0,
            h0,
            EVEREST_1830_ELLIPSOID,
            WGS84_ELLIPSOID,
            EVEREST_1830_TO_WGS84_SOI_7P,
            inverse=False,
        )
        lon_back, lat_back, h_back = HelmertTransform.transform_geodetic(
            lon_wgs,
            lat_wgs,
            h_wgs,
            WGS84_ELLIPSOID,
            EVEREST_1830_ELLIPSOID,
            EVEREST_1830_TO_WGS84_SOI_7P,
            inverse=True,
        )
        assert lon_back == pytest.approx(lon0, abs=1e-9)
        assert lat_back == pytest.approx(lat0, abs=1e-9)
        assert h_back == pytest.approx(h0, abs=1e-4)
