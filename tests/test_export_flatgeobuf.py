"""Tests for FlatGeobuf binary export, streaming chunks, and Packed R-Tree spatial indexing (Step 105/150).

Directive Sections 26-28:
- Harmonized cadastral layers are serialized into spec-compliant FlatGeobuf (`.fgb`, magic bytes `fgb\\x03fgb\\x00`)
  with a Hilbert-ordered Packed R-Tree spatial index (`index_node_size = 16`) in canonical WGS 84 (`EPSG:4326`).
- Output round-trips cleanly through `flatgeobuf.load(...)` and `flatgeobuf.Reader(..., bbox=...)`, enabling indexed
  spatial bbox reads without scanning the full dataset.
- Audit-only merged slivers (`attributes["merged_into_parcel_id"]`) are excluded from active export by default unless
  `include_merged_slivers=True`.
- Output is deterministic regardless of input parcel order, and supports chunked async/sync streaming.
"""

from __future__ import annotations

import io
import uuid

import flatgeobuf
import pytest
from flatgeobuf.constants import magicbytes as FGB_MAGIC
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon, shape

from backend.export.flatgeobuf import (
    FlatGeobufExportMetadata,
    build_flatgeobuf_metadata,
    export_parcels_to_flatgeobuf,
    read_flatgeobuf_features,
    stream_flatgeobuf_bytes,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84

LAYER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_C = uuid.UUID("00000000-0000-0000-0000-000000000003")
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


class TestFlatGeobufExportAndSpatialIndex:
    def test_produces_valid_flatgeobuf_with_magic_bytes_and_roundtrips_features(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        p_b = _orm_parcel(
            ID_B,
            "P-002",
            _rect(100.0, 0.0, 200.0, 100.0),
            locked=True,
            status=ParcelResolutionStatus.DISPUTED.value,
        )

        fgb_bytes = export_parcels_to_flatgeobuf([p_a, p_b], layer_name="ward_14")

        assert isinstance(fgb_bytes, bytes)
        assert fgb_bytes.startswith(FGB_MAGIC)

        fc = flatgeobuf.load(io.BytesIO(fgb_bytes))
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) == 2

        by_ident = {feat["properties"]["parcel_identifier"]: feat for feat in fc["features"]}
        assert set(by_ident) == {"P-001", "P-002"}

        f_a = by_ident["P-001"]
        assert f_a["geometry"]["type"] == "MultiPolygon"
        assert shape(f_a["geometry"]).is_valid
        assert f_a["properties"]["parcel_id"] == str(ID_A)
        assert f_a["properties"]["metric_area_sqm"] == pytest.approx(10_000.0, abs=1e-4)
        assert f_a["properties"]["dispute_locked"] is False
        assert f_a["properties"]["resolution_status"] == ParcelResolutionStatus.AUTO_RESOLVED.value

        f_b = by_ident["P-002"]
        assert f_b["properties"]["dispute_locked"] is True
        assert f_b["properties"]["is_disputed"] is True
        assert f_b["properties"]["resolution_status"] == ParcelResolutionStatus.DISPUTED.value

    def test_packed_rtree_spatial_index_filters_by_bbox(self):
        # Place three spatially separated parcels (~20 km apart) so WGS84 bboxes are cleanly disjoint
        p_a = _orm_parcel(ID_A, "WEST", _rect(0.0, 0.0, 100.0, 100.0, dx=500_000.0, dy=3_000_000.0))
        p_b = _orm_parcel(ID_B, "CENTER", _rect(0.0, 0.0, 100.0, 100.0, dx=520_000.0, dy=3_000_000.0))
        p_c = _orm_parcel(ID_C, "EAST", _rect(0.0, 0.0, 100.0, 100.0, dx=540_000.0, dy=3_000_000.0))

        fgb_bytes = export_parcels_to_flatgeobuf([p_a, p_b, p_c])

        # Query only the bounding box around CENTER parcel
        center_bounds = project_metric_geometry_to_wgs84(
            _rect(-10.0, -10.0, 110.0, 110.0, dx=520_000.0, dy=3_000_000.0)
        ).bounds

        filtered = read_flatgeobuf_features(fgb_bytes, bbox=center_bounds)
        assert len(filtered) == 1
        assert filtered[0]["properties"]["parcel_identifier"] == "CENTER"

        # Empty-intersection bbox returns 0 features
        far_bbox = (10.0, 10.0, 11.0, 11.0)
        assert read_flatgeobuf_features(fgb_bytes, bbox=far_bbox) == []

    def test_excludes_merged_audit_slivers_by_default(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        p_s = _orm_parcel(
            ID_S,
            "S-001",
            _rect(-0.1, 0.0, 0.0, 40.0),
            sliver=True,
            attributes={"merged_into_parcel_id": str(ID_A)},
        )

        default_fgb = export_parcels_to_flatgeobuf([p_a, p_s])
        assert len(read_flatgeobuf_features(default_fgb)) == 1

        all_fgb = export_parcels_to_flatgeobuf([p_a, p_s], include_merged_slivers=True)
        assert len(read_flatgeobuf_features(all_fgb)) == 2

    def test_empty_parcel_list_produces_valid_empty_flatgeobuf(self):
        fgb_bytes = export_parcels_to_flatgeobuf([])
        assert fgb_bytes.startswith(FGB_MAGIC)
        assert read_flatgeobuf_features(fgb_bytes) == []

    def test_export_is_byte_identical_regardless_of_input_order(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        p_b = _orm_parcel(ID_B, "P-002", _rect(100.0, 0.0, 200.0, 100.0))
        p_c = _orm_parcel(ID_C, "P-003", _rect(200.0, 0.0, 300.0, 100.0))

        first = export_parcels_to_flatgeobuf([p_a, p_b, p_c])
        second = export_parcels_to_flatgeobuf([p_c, p_a, p_b])
        assert first == second

    def test_metadata_and_chunked_streaming_reassemble_exact_bytes(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        p_b = _orm_parcel(ID_B, "P-002", _rect(100.0, 0.0, 200.0, 100.0))

        fgb_bytes = export_parcels_to_flatgeobuf([p_a, p_b])
        meta = build_flatgeobuf_metadata([p_a, p_b], fgb_bytes)

        assert isinstance(meta, FlatGeobufExportMetadata)
        assert meta.feature_count == 2
        assert meta.byte_length == len(fgb_bytes)
        assert meta.srid == 4326
        assert meta.bbox is not None and len(meta.bbox) == 4

        chunks = list(stream_flatgeobuf_bytes(fgb_bytes, chunk_size=256))
        assert len(chunks) >= 2
        assert b"".join(chunks) == fgb_bytes

    def test_invalid_bbox_raises_value_error(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        fgb_bytes = export_parcels_to_flatgeobuf([p_a])

        with pytest.raises(ValueError, match="bbox"):
            read_flatgeobuf_features(fgb_bytes, bbox=(80.0, 20.0, 70.0, 30.0))
