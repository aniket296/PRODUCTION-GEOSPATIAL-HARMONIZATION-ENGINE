"""Tests for OGC GeoPackage (.gpkg) export format and attribute retention (Step 109/150).

Directive Sections 26-28:
- Harmonized cadastral layers export to OGC GeoPackage (`.gpkg`) SQLite containers with `PRAGMA application_id = 0x47504B47`
  (`"GPKG"`), `gpkg_spatial_ref_sys`, `gpkg_contents`, and `gpkg_geometry_columns` metadata tables.
- Feature geometries are stored in OGC GeoPackage Binary (GPB) format (`b"GP"` header + envelope + WKB) in WGS 84
  (`EPSG:4326`) by default (or projected `EPSG:32643` when requested via `srid=32643`).
- All core cadastral metrics, resolution/dispute flags, and custom source `attributes` JSON are retained losslessly.
- Merged audit slivers (`attributes["merged_into_parcel_id"]`) are excluded by default unless `include_merged_slivers=True`.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
import uuid
from pathlib import Path

import pytest
from geoalchemy2.shape import from_shape
from shapely import wkb as shapely_wkb
from shapely.geometry import MultiPolygon, Polygon

from backend.export.geopackage import (
    GPKG_APPLICATION_ID,
    GeoPackageExportMetadata,
    build_geopackage_metadata,
    export_parcels_to_geopackage,
    read_geopackage_features,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84

LAYER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_S = uuid.UUID("00000000-0000-0000-0000-000000000099")

DX, DY = 500_000.0, 3_000_000.0


def _rect(x0: float, y0: float, x1: float, y1: float, dx: float = DX, dy: float = DY) -> Polygon:
    return Polygon(
        [(x0 + dx, y0 + dy), (x1 + dx, y0 + dy), (x1 + dx, y1 + dy), (x0 + dx, y1 + dy), (x0 + dx, y0 + dy)]
    )


def _orm_parcel(
    pid: uuid.UUID,
    ident: str,
    poly: Polygon,
    *,
    locked: bool = False,
    sliver: bool = False,
    status: str = ParcelResolutionStatus.AUTO_RESOLVED.value,
    attributes: dict | None = None,
) -> HarmonizedParcel:
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=pid,
        layer_id=LAYER_ID,
        parcel_identifier=ident,
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        polsby_popper_compactness=HarmonizedParcel.calculate_polsby_popper(float(poly.area), float(poly.length)),
        is_sliver=sliver,
        is_disputed=locked,
        dispute_locked=locked,
        resolution_status=status,
        attributes=attributes,
    )


class TestGeoPackageExportAndAttributes:
    def test_produces_valid_sqlite_gpkg_with_ogc_metadata_tables(self):
        p_a = _orm_parcel(
            ID_A,
            "Khasra-101/A",
            _rect(0.0, 0.0, 100.0, 100.0),
            attributes={"village": "Rampur", "khata": 42},
        )
        p_b = _orm_parcel(
            ID_B,
            "Khasra-102",
            _rect(100.0, 0.0, 200.0, 100.0),
            locked=True,
            status=ParcelResolutionStatus.DISPUTED.value,
            attributes={"dispute_reason": "CRITICAL_DISPUTE"},
        )

        gpkg_bytes = export_parcels_to_geopackage([p_a, p_b], table_name="ward_14_parcels")

        assert isinstance(gpkg_bytes, bytes)
        assert gpkg_bytes.startswith(b"SQLite format 3\x00")

        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
            tmp.write(gpkg_bytes)
            tmp_path = Path(tmp.name)

        try:
            conn = sqlite3.connect(str(tmp_path))
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            assert app_id == GPKG_APPLICATION_ID

            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert {"gpkg_spatial_ref_sys", "gpkg_contents", "gpkg_geometry_columns", "ward_14_parcels"} <= tables

            contents = conn.execute(
                "SELECT table_name, data_type, srs_id FROM gpkg_contents WHERE table_name='ward_14_parcels'"
            ).fetchone()
            assert contents == ("ward_14_parcels", "features", 4326)

            geom_col = conn.execute(
                "SELECT column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns WHERE table_name='ward_14_parcels'"
            ).fetchone()
            assert geom_col == ("geom", "MULTIPOLYGON", 4326)

            rows = conn.execute("SELECT geom, parcel_identifier, attributes_json FROM ward_14_parcels ORDER BY fid").fetchall()
            assert len(rows) == 2

            # Verify OGC GeoPackage Binary (GPB) header + valid WKB payload
            gpb_blob = rows[0][0]
            assert gpb_blob[:2] == b"GP"
            srid_val = struct.unpack_from("<i", gpb_blob, 4)[0]
            assert srid_val == 4326
            wkb_payload = gpb_blob[40:]
            geom = shapely_wkb.loads(wkb_payload)
            assert geom.geom_type == "MultiPolygon"
            assert geom.is_valid
        finally:
            conn.close()
            tmp_path.unlink(missing_ok=True)

    def test_retains_all_parcel_metrics_flags_and_custom_attributes(self):
        p_a = _orm_parcel(
            ID_A,
            "P-001",
            _rect(0.0, 0.0, 100.0, 100.0),
            attributes={"owner": "Sita Devi", "survey_no": "17/2", "ward": 14},
        )
        p_b = _orm_parcel(
            ID_B,
            "P-002",
            _rect(100.0, 0.0, 200.0, 100.0),
            locked=True,
            status=ParcelResolutionStatus.DISPUTED.value,
        )

        gpkg_bytes = export_parcels_to_geopackage([p_b, p_a])
        features = read_geopackage_features(gpkg_bytes)

        assert len(features) == 2
        by_ident = {f["properties"]["parcel_identifier"]: f for f in features}

        fa = by_ident["P-001"]
        assert fa["geometry"].geom_type == "MultiPolygon"
        assert fa["properties"]["parcel_id"] == str(ID_A)
        assert fa["properties"]["metric_area_sqm"] == pytest.approx(10_000.0, abs=1e-4)
        assert fa["properties"]["metric_perimeter_m"] == pytest.approx(400.0, abs=1e-4)
        assert fa["properties"]["dispute_locked"] is False
        assert fa["properties"]["resolution_status"] == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert json.loads(fa["properties"]["attributes_json"]) == {"owner": "Sita Devi", "survey_no": "17/2", "ward": 14}

        fb = by_ident["P-002"]
        assert fb["properties"]["dispute_locked"] is True
        assert fb["properties"]["is_disputed"] is True
        assert fb["properties"]["resolution_status"] == ParcelResolutionStatus.DISPUTED.value

    def test_supports_projected_srid_32643_and_excludes_merged_slivers_by_default(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        p_s = _orm_parcel(
            ID_S,
            "S-001",
            _rect(-0.1, 0.0, 0.0, 40.0),
            sliver=True,
            attributes={"merged_into_parcel_id": str(ID_A)},
        )

        gpkg_default = export_parcels_to_geopackage([p_a, p_s], srid=32643)
        feats_default = read_geopackage_features(gpkg_default)
        assert len(feats_default) == 1
        assert feats_default[0]["geometry"].area == pytest.approx(10_000.0, abs=1e-6)

        gpkg_all = export_parcels_to_geopackage([p_a, p_s], srid=32643, include_merged_slivers=True)
        assert len(read_geopackage_features(gpkg_all)) == 2

    def test_empty_layer_produces_valid_empty_geopackage_and_metadata(self):
        gpkg_bytes = export_parcels_to_geopackage([])
        assert gpkg_bytes.startswith(b"SQLite format 3\x00")
        assert read_geopackage_features(gpkg_bytes) == []

        meta = build_geopackage_metadata([], gpkg_bytes)
        assert isinstance(meta, GeoPackageExportMetadata)
        assert meta.feature_count == 0
        assert meta.byte_length == len(gpkg_bytes)
        assert meta.srid == 4326

    def test_invalid_srid_or_table_name_raises_value_error(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        with pytest.raises(ValueError, match="srid"):
            export_parcels_to_geopackage([p_a], srid=3857)
        with pytest.raises(ValueError, match="table_name"):
            export_parcels_to_geopackage([p_a], table_name="bad; DROP TABLE parcels;--")
