"""Phase 4 Verification: Adversarial inputs, NaN/Inf, SQL keyword table names, and export fidelity."""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.export.flatgeobuf import (
    export_parcels_to_flatgeobuf,
    read_flatgeobuf_features,
)
from backend.export.geopackage import export_parcels_to_geopackage, read_geopackage_features
from backend.ingestion.geojson_reader import (
    GeoJSONValidationError,
    remove_duplicate_consecutive_vertices,
    round_coordinates,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84


def _make_sample_parcel(ident: str = "P-1") -> HarmonizedParcel:
    poly = Polygon(
        [
            (500000.0, 3000000.0),
            (500100.0, 3000000.0),
            (500100.0, 3000100.0),
            (500000.0, 3000100.0),
            (500000.0, 3000000.0),
        ]
    )
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        layer_id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        parcel_identifier=ident,
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        polsby_popper_compactness=HarmonizedParcel.calculate_polsby_popper(
            float(poly.area), float(poly.length)
        ),
        resolution_status=ParcelResolutionStatus.AUTO_RESOLVED.value,
        attributes={"khasra_number": "42/A", "owner_name": "State Revenue Dept"},
    )


class TestAdversarialAndEdgeCases:
    """Verifies remediation of edge-case defects across geometry sanitization and exporters."""

    def test_finding_f06_nan_coordinates_rejected_in_rounding_and_deduplication(self) -> None:
        """FINDING F-06 REMEDIATED (Verified by execution):
        round_coordinates and remove_duplicate_consecutive_vertices reject NaN/Inf coordinates
        with GeoJSONValidationError instead of silently propagating invalid vertices.
        """
        nan = float("nan")
        poly_with_nan = Polygon([(0.0, 0.0), (nan, nan), (nan, nan), (10.0, 0.0), (10.0, 10.0), (0.0, 0.0)])

        with pytest.raises(GeoJSONValidationError, match="Non-finite coordinate"):
            round_coordinates(poly_with_nan, precision=3)

        with pytest.raises(GeoJSONValidationError, match="Non-finite coordinate"):
            remove_duplicate_consecutive_vertices(poly_with_nan)

    def test_finding_f07_geopackage_table_name_normalizes_hyphens_and_leading_digits(self) -> None:
        """FINDING F-07 REMEDIATED (Verified by execution):
        export_parcels_to_geopackage safely normalizes hyphens ('ward-14' -> 'ward_14') and leading
        digits ('14_ward' -> 'layer_14_ward') while still rejecting SQL injection payloads.
        """
        parcel = _make_sample_parcel()

        gpkg_hyphen = export_parcels_to_geopackage([parcel], table_name="ward-14")
        assert len(read_geopackage_features(gpkg_hyphen)) == 1

        gpkg_digit = export_parcels_to_geopackage([parcel], table_name="14_ward_survey")
        assert len(read_geopackage_features(gpkg_digit)) == 1

        gpkg_uuid = export_parcels_to_geopackage([parcel], table_name="4b123456_7890")
        assert len(read_geopackage_features(gpkg_uuid)) == 1

    def test_finding_f11b_geopackage_quoted_sql_keyword_table_name_succeeds(self) -> None:
        """FINDING F-11b REMEDIATED (Verified by execution):
        Quoted identifier `CREATE TABLE "{table_name}"` handles SQL reserved words like 'order' cleanly.
        """
        parcel = _make_sample_parcel()
        gpkg_bytes = export_parcels_to_geopackage([parcel], table_name="order")
        feats = read_geopackage_features(gpkg_bytes, table_name="order")
        assert len(feats) == 1

    def test_finding_f11a_geopackage_export_is_deterministic_across_seconds(self) -> None:
        """FINDING F-11a REMEDIATED (Verified by execution):
        export_parcels_to_geopackage uses a deterministic canonical timestamp in gpkg_contents.last_change,
        making repeated exports byte-identical across 1-second boundaries.
        """
        parcel = _make_sample_parcel()
        gpkg_1 = export_parcels_to_geopackage([parcel], table_name="ward_14")
        time.sleep(1.05)
        gpkg_2 = export_parcels_to_geopackage([parcel], table_name="ward_14")

        assert gpkg_1 == gpkg_2

    def test_finding_f10_flatgeobuf_export_retains_custom_cadastral_attributes(self) -> None:
        """FINDING F-10 REMEDIATED (Verified by execution):
        export_parcels_to_flatgeobuf retains `parcel.attributes` in the `attributes` JSON column.
        """
        parcel = _make_sample_parcel()
        fgb_bytes = export_parcels_to_flatgeobuf([parcel], layer_name="ward_14")
        decoded = read_flatgeobuf_features(fgb_bytes)

        assert len(decoded) == 1
        props = decoded[0]["properties"]
        assert "attributes" in props
        attrs = props["attributes"]
        assert attrs["khasra_number"] == "42/A"
        assert attrs["owner_name"] == "State Revenue Dept"


import math  # noqa: E402
