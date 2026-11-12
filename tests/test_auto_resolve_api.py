"""Integration and regression tests for POST /api/v1/layers/{layer_id}/auto-resolve (Step 104/150)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon
from starlette.testclient import TestClient

from backend.api.app import app
from backend.db.session import get_async_session
from backend.models.layer import LayerStatus, RawCadastralLayer
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.topology.intersection import extract_polygonal_intersection
from backend.topology.recorder import TopologyConflictRecorder, project_metric_geometry_to_wgs84

DX, DY = 500_000.0, 3_000_000.0


def _rect(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return Polygon(
        [(x0 + DX, y0 + DY), (x1 + DX, y0 + DY), (x1 + DX, y1 + DY), (x0 + DX, y1 + DY), (x0 + DX, y0 + DY)]
    )


def _make_layer(layer_id: uuid.UUID) -> RawCadastralLayer:
    layer = RawCadastralLayer(
        name="Auto Resolve Test Layer",
        filename="auto_resolve.geojson",
        file_format="GEOJSON",
        storage_path="/tmp/auto_resolve.geojson",
        source_crs="EPSG:32643",
        target_crs="EPSG:4326",
        status=LayerStatus.COMPLETED.value,
    )
    layer.id = layer_id
    return layer


def _make_parcel(
    layer_id: uuid.UUID,
    parcel_id: uuid.UUID,
    ident: str,
    poly: Polygon,
    *,
    sliver: bool = False,
) -> HarmonizedParcel:
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=parcel_id,
        layer_id=layer_id,
        parcel_identifier=ident,
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        is_sliver=sliver,
    )


def _mock_session_for_auto_resolve(
    layer: RawCadastralLayer | None,
    parcels: list[HarmonizedParcel],
    errors: list[TopologicalError],
) -> AsyncMock:
    session = AsyncMock()

    layer_res = MagicMock()
    layer_res.scalar_one_or_none.return_value = layer

    parcel_res = MagicMock()
    parcel_scalars = MagicMock()
    parcel_scalars.all.return_value = parcels
    parcel_res.scalars.return_value = parcel_scalars

    err_res = MagicMock()
    err_scalars = MagicMock()
    err_scalars.all.return_value = errors
    err_res.scalars.return_value = err_scalars

    session.execute.side_effect = [layer_res, parcel_res, err_res]
    return session


class TestAutoResolveEndpoint:
    def test_auto_resolve_heals_slivers_and_micro_overlaps_while_locking_disputes(self):
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)

        id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
        id_c = uuid.UUID("00000000-0000-0000-0000-000000000003")
        id_d = uuid.UUID("00000000-0000-0000-0000-000000000004")
        id_s = uuid.UUID("00000000-0000-0000-0000-000000000099")

        # A & B: 1% micro-overlap; S: 4 m^2 sliver attached to A; C & D: 10% critical dispute
        p_a = _make_parcel(layer_id, id_a, "A", _rect(0.0, 0.0, 100.0, 100.0))
        p_b = _make_parcel(layer_id, id_b, "B", _rect(99.0, 0.0, 199.0, 100.0))
        p_s = _make_parcel(layer_id, id_s, "S", _rect(-0.1, 0.0, 0.0, 40.0), sliver=True)
        p_c = _make_parcel(layer_id, id_c, "C", _rect(400.0, 400.0, 500.0, 500.0))
        p_d = _make_parcel(layer_id, id_d, "D", _rect(490.0, 400.0, 590.0, 500.0))

        c_wkb_before = bytes(p_c.geometry_projected.data)
        d_wkb_before = bytes(p_d.geometry_projected.data)

        recorder = TopologyConflictRecorder()
        errors = recorder.build_layer_errors(layer_id, [p_a, p_b, p_s, p_c, p_d])
        for err in errors:
            if err.id is None:
                err.id = uuid.uuid4()
            if err.created_at is None:
                err.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        mock_session = _mock_session_for_auto_resolve(layer, [p_a, p_b, p_c, p_d, p_s], errors)

        async def _override_session():
            yield mock_session

        app.dependency_overrides[get_async_session] = _override_session
        try:
            with TestClient(app) as client:
                resp = client.post(
                    f"/api/v1/layers/{layer_id}/auto-resolve",
                    json={"resolved_by": "test-reviewer", "strict_verification": True},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["layer_id"] == str(layer_id)
        assert body["status"] == "COMPLETED_WITH_DISPUTES"
        assert body["total_parcels"] == 5
        assert body["slivers_merged_count"] == 1
        assert body["micro_overlaps_resolved_count"] == 1
        assert body["residual_auto_resolvable_count"] == 0
        assert body["resolved_errors_count"] >= 2
        assert body["remaining_disputes_count"] >= 1
        assert set(body["dispute_locked_parcel_ids"]) == {str(id_c), str(id_d)}
        assert set(body["auto_resolved_parcel_ids"]) == {str(id_a), str(id_b), str(id_s)}
        assert "verified_at" in body

        # Micro-overlap healed completely
        assert extract_polygonal_intersection(to_shape(p_a.geometry_projected), to_shape(p_b.geometry_projected)).area == pytest.approx(0.0, abs=1e-6)
        assert p_a.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert p_b.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert p_s.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value

        # Critical dispute geometries preserved byte-for-byte
        assert bytes(p_c.geometry_projected.data) == c_wkb_before
        assert bytes(p_d.geometry_projected.data) == d_wkb_before
        assert p_c.dispute_locked is True
        assert p_d.dispute_locked is True

        # Audit records stamped properly
        auto_errs = [e for e in errors if e.severity == TopologySeverity.AUTO_RESOLVABLE.value]
        crit_errs = [e for e in errors if e.severity == TopologySeverity.CRITICAL_DISPUTE.value]
        assert auto_errs and all(e.is_resolved and e.resolved_by == "test-reviewer" and e.resolved_at is not None for e in auto_errs)
        assert crit_errs and all((not e.is_resolved) and e.resolved_at is None for e in crit_errs)

        mock_session.commit.assert_awaited_once()
        mock_session.rollback.assert_not_awaited()

    def test_auto_resolve_empty_layer_with_no_body_returns_completed(self):
        layer_id = uuid.uuid4()
        mock_session = _mock_session_for_auto_resolve(_make_layer(layer_id), [], [])

        async def _override_session():
            yield mock_session

        app.dependency_overrides[get_async_session] = _override_session
        try:
            with TestClient(app) as client:
                resp = client.post(f"/api/v1/layers/{layer_id}/auto-resolve")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "COMPLETED"
        assert body["total_parcels"] == 0
        assert body["healed_parcel_count"] == 0
        mock_session.commit.assert_awaited_once()

    def test_auto_resolve_returns_422_and_rolls_back_on_collapsing_geometry(self):
        layer_id = uuid.uuid4()
        p_a = _make_parcel(layer_id, uuid.uuid4(), "A", _rect(0.0, 0.0, 100.0, 100.0))
        p_b = _make_parcel(layer_id, uuid.uuid4(), "B", _rect(99.0, 0.0, 199.0, 100.0))
        # Collapses on 1 mm grid snap -> triggers HealingTransactionRollbackError
        p_bad = _make_parcel(layer_id, uuid.uuid4(), "BAD", _rect(500.0001, 500.0001, 500.0003, 500.0003))

        a_wkb_before = bytes(p_a.geometry_projected.data)
        b_wkb_before = bytes(p_b.geometry_projected.data)
        mock_session = _mock_session_for_auto_resolve(_make_layer(layer_id), [p_a, p_b, p_bad], [])

        async def _override_session():
            yield mock_session

        app.dependency_overrides[get_async_session] = _override_session
        try:
            with TestClient(app) as client:
                resp = client.post(f"/api/v1/layers/{layer_id}/auto-resolve")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
        assert bytes(p_a.geometry_projected.data) == a_wkb_before
        assert bytes(p_b.geometry_projected.data) == b_wkb_before
        assert p_a.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        assert p_b.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        mock_session.rollback.assert_awaited_once()
        mock_session.commit.assert_not_awaited()

    def test_auto_resolve_returns_404_for_missing_layer(self):
        layer_id = uuid.uuid4()
        mock_session = AsyncMock()
        layer_res = MagicMock()
        layer_res.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = layer_res

        async def _override_session():
            yield mock_session

        app.dependency_overrides[get_async_session] = _override_session
        try:
            with TestClient(app) as client:
                resp = client.post(f"/api/v1/layers/{layer_id}/auto-resolve")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 404

    def test_auto_resolve_returns_422_on_invalid_uuid(self):
        with TestClient(app) as client:
            resp = client.post("/api/v1/layers/not-a-uuid/auto-resolve")
        assert resp.status_code == 422

    def test_auto_resolve_returns_503_when_database_unavailable(self):
        layer_id = uuid.uuid4()

        async def _override_none():
            yield None

        app.dependency_overrides[get_async_session] = _override_none
        try:
            with TestClient(app) as client:
                resp = client.post(f"/api/v1/layers/{layer_id}/auto-resolve")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 503
