"""Tests for RFC 7946 GeoJSON export and coordinate winding order (Step 111/150).

Directive Sections 26-28:
- Harmonized cadastral layers export to strict RFC 7946 GeoJSON `FeatureCollection` in WGS 84 (`EPSG:4326`).
- Winding order (RFC 7946 Section 3.1.6):
  - Exterior rings MUST be counter-clockwise (signed area > 0).
  - Interior hole rings MUST be clockwise (signed area < 0).
  - All rings are closed (`coords[0] == coords[-1]`).
- FeatureCollection includes a 4-element `bbox` `[min_lon, min_lat, max_lon, max_lat]` when non-empty, preserves all
  cadastral metrics, flags, and custom `attributes`, excludes merged audit slivers by default, and produces
  deterministic output regardless of input parcel order.
"""

from __future__ import annotations

import json
import uuid

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon, shape

from backend.export.geojson import (
    export_parcels_to_geojson_bytes,
    export_parcels_to_geojson_dict,
    signed_ring_area,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84

LAYER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_S = uuid.UUID("00000000-0000-0000-0000-000000000099")

DX, DY = 500_000.0, 3_000_000.0


def _cw_rect_with_ccw_hole() -> Polygon:
    """Construct a polygon with deliberately reversed (CW) exterior and (CCW) hole in UTM 43N."""
    # Clockwise exterior
    exterior = [
        (DX + 0.0, DY + 0.0),
        (DX + 0.0, DY + 100.0),
        (DX + 100.0, DY + 100.0),
        (DX + 100.0, DY + 0.0),
        (DX + 0.0, DY + 0.0),
    ]
    # Counter-clockwise interior hole
    hole = [
        (DX + 20.0, DY + 20.0),
        (DX + 80.0, DY + 20.0),
        (DX + 80.0, DY + 80.0),
        (DX + 20.0, DY + 80.0),
        (DX + 20.0, DY + 20.0),
    ]
    return Polygon(exterior, [hole])


def _rect(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return Polygon([(DX + x0, DY + y0), (DX + x1, DY + y0), (DX + x1, DY + y1), (DX + x0, DY + y1), (DX + x0, DY + y0)])


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


class TestRFC7946GeoJSONExport:
    def test_enforces_rfc7946_ccw_exterior_and_cw_interior_winding_order(self):
        parcel = _orm_parcel(
            ID_A,
            "Khasra-Hole-01",
            _cw_rect_with_ccw_hole(),
            attributes={"ward": 14},
        )

        fc = export_parcels_to_geojson_dict([parcel])
        assert fc["type"] == "FeatureCollection"
        assert "crs" not in fc  # RFC 7946 deprecates the legacy "crs" member
        assert len(fc["features"]) == 1

        geom = fc["features"][0]["geometry"]
        assert geom["type"] == "MultiPolygon"
        poly_coords = geom["coordinates"][0]
        assert len(poly_coords) == 2  # exterior + 1 hole

        exterior_ring, hole_ring = poly_coords[0], poly_coords[1]
        assert exterior_ring[0] == exterior_ring[-1]
        assert hole_ring[0] == hole_ring[-1]
        assert signed_ring_area(exterior_ring) > 0.0
        assert signed_ring_area(hole_ring) < 0.0
        assert shape(geom).is_valid

    def test_retains_metrics_flags_attributes_and_wgs84_bbox(self):
        p_a = _orm_parcel(
            ID_A,
            "P-001",
            _rect(0.0, 0.0, 100.0, 100.0),
            attributes={"owner": "Mohan", "khata": 19},
        )
        p_b = _orm_parcel(
            ID_B,
            "P-002",
            _rect(100.0, 0.0, 200.0, 100.0),
            locked=True,
            status=ParcelResolutionStatus.DISPUTED.value,
        )

        payload = export_parcels_to_geojson_bytes([p_b, p_a])
        fc = json.loads(payload.decode("utf-8"))

        assert fc["type"] == "FeatureCollection"
        assert len(fc["bbox"]) == 4
        min_lon, min_lat, max_lon, max_lat = fc["bbox"]
        assert 68.0 <= min_lon < max_lon <= 97.0
        assert 6.0 <= min_lat < max_lat <= 38.0

        assert [f["id"] for f in fc["features"]] == [str(ID_A), str(ID_B)]
        props_a = fc["features"][0]["properties"]
        assert props_a["parcel_id"] == str(ID_A)
        assert props_a["parcel_identifier"] == "P-001"
        assert props_a["metric_area_sqm"] == pytest.approx(10_000.0, abs=1e-4)
        assert props_a["dispute_locked"] is False
        assert props_a["attributes"] == {"owner": "Mohan", "khata": 19}

        props_b = fc["features"][1]["properties"]
        assert props_b["dispute_locked"] is True
        assert props_b["resolution_status"] == ParcelResolutionStatus.DISPUTED.value

    def test_excludes_merged_audit_slivers_by_default_and_is_deterministic(self):
        p_a = _orm_parcel(ID_A, "P-001", _rect(0.0, 0.0, 100.0, 100.0))
        p_b = _orm_parcel(ID_B, "P-002", _rect(100.0, 0.0, 200.0, 100.0))
        p_s = _orm_parcel(
            ID_S,
            "S-001",
            _rect(-0.1, 0.0, 0.0, 40.0),
            sliver=True,
            attributes={"merged_into_parcel_id": str(ID_A)},
        )

        bytes_1 = export_parcels_to_geojson_bytes([p_a, p_b, p_s])
        bytes_2 = export_parcels_to_geojson_bytes([p_s, p_b, p_a])
        assert bytes_1 == bytes_2
        assert len(json.loads(bytes_1)["features"]) == 2

        all_fc = export_parcels_to_geojson_dict([p_a, p_b, p_s], include_merged_slivers=True)
        assert len(all_fc["features"]) == 3

    def test_empty_layer_returns_empty_feature_collection(self):
        fc = export_parcels_to_geojson_dict([])
        assert fc == {"type": "FeatureCollection", "features": []}
