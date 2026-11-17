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
from backend.export.geopackage import export_parcels_to_geopackage
from backend.ingestion.geojson_reader import (
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
    """Reproduces edge-case bugs across geometry sanitization and exporters."""

    def test_finding_f06_nan_coordinates_survive_rounding_and_deduplication(self) -> None:
        """FINDING F-06 (Verified by execution):
        round_coordinates and remove_duplicate_consecutive_vertices do not reject NaN/Inf
        coordinates; consecutive (nan, nan) points are not deduplicated because (nan, nan) != (nan, nan).
        """
        nan = float("nan")
        poly_with_nan = Polygon([(0.0, 0.0), (nan, nan), (nan, nan), (10.0, 0.0), (10.0, 10.0), (0.0, 0.0)])

        rounded = round_coordinates(poly_with_nan, precision=3)
        deduped = remove_duplicate_consecutive_vertices(rounded)

        # Both consecutive NaN vertices remain in the ring because (nan, nan) != (nan, nan) is True!
        coords = list(deduped.exterior.coords)
        nan_count = sum(1 for x, y in coords if math.isnan(x) or math.isnan(y))
        assert nan_count == 2

    def test_finding_f07_geopackage_table_name_rejects_hyphens_and_leading_digits(self) -> None:
        """FINDING F-07 (Verified by execution):
        export_parcels_to_geopackage rejects table names with hyphens ('ward-14') or leading
        digits ('14_ward' or UUID strings starting with 0-9), which staging.py / bundle.py / layers.py produce.
        """
        parcel = _make_sample_parcel()

        with pytest.raises(ValueError, match="Invalid GeoPackage table_name"):
            export_parcels_to_geopackage([parcel], table_name="ward-14")

        with pytest.raises(ValueError, match="Invalid GeoPackage table_name"):
            export_parcels_to_geopackage([parcel], table_name="14_ward_survey")

        with pytest.raises(ValueError, match="Invalid GeoPackage table_name"):
            export_parcels_to_geopackage([parcel], table_name="4b123456_7890")

    def test_finding_f11b_geopackage_unquoted_sql_keyword_table_name_crashes_sqlite(self) -> None:
        """FINDING F-11b (Verified by execution):
        _validate_table_name('order') passes regex validation, but unquoted
        `CREATE TABLE {table_name}` crashes with sqlite3.OperationalError on SQL reserved words.
        """
        parcel = _make_sample_parcel()

        with pytest.raises(sqlite3.OperationalError, match='near "order": syntax error'):
            export_parcels_to_geopackage([parcel], table_name="order")

    def test_finding_f11a_geopackage_export_is_non_deterministic_across_seconds(self) -> None:
        """FINDING F-11a (Verified by execution):
        export_parcels_to_geopackage embeds datetime.now(timezone.utc) into gpkg_contents.last_change,
        making repeated exports across a 1-second boundary non-identical in bytes and SHA-256.
        """
        parcel = _make_sample_parcel()
        gpkg_1 = export_parcels_to_geopackage([parcel], table_name="ward_14")
        time.sleep(1.05)
        gpkg_2 = export_parcels_to_geopackage([parcel], table_name="ward_14")

        assert gpkg_1 != gpkg_2

    def test_finding_f10_flatgeobuf_export_drops_custom_cadastral_attributes(self) -> None:
        """FINDING F-10 (Verified by execution):
        export_parcels_to_flatgeobuf encodes only fixed metric/flag columns and silently drops
        `parcel.attributes` (e.g., khasra_number, owner_name).
        """
        parcel = _make_sample_parcel()
        fgb_bytes = export_parcels_to_flatgeobuf([parcel], layer_name="ward_14")
        decoded = read_flatgeobuf_features(fgb_bytes)

        assert len(decoded) == 1
        props = decoded[0]["properties"]
        assert "khasra_number" not in props
        assert "owner_name" not in props
        assert "attributes" not in props


import math  # noqa: E402
