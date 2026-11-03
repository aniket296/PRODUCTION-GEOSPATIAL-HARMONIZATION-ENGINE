"""Tests for GET /api/v1/layers/{layer_id}/topology-errors GeoJSON FeatureCollection API (Step 86/150).

Validates:
1. RFC 7946 GeoJSON `FeatureCollection` structure (`TopologyErrorFeatureCollection`, `TopologyErrorFeature`,
   `TopologyErrorProperties`) and WGS 84 (`EPSG:4326`) bounding box `[min_lon, min_lat, max_lon, max_lat]`
   computation across heterogeneous conflict geometries (`MultiPolygon`, `Point`).
2. Summary counters (`total_count`, `auto_resolvable_count`, `critical_dispute_count`, `resolved_count`,
   `unresolved_count`, `counts_by_type`).
3. FastAPI endpoint `GET /api/v1/layers/{layer_id}/topology-errors`:
   - HTTP 200 with full conflict list and with query filters (`severity`, `error_type`, `is_resolved`).
   - HTTP 200 with empty `features: []` and `bbox: None` when layer has zero conflicts.
   - HTTP 404 when the requested `layer_id` does not exist.
   - HTTP 422 when `layer_id`, `severity`, or `error_type` is malformed.
   - HTTP 503 when the database session is unavailable (`None`).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Point, Polygon
from starlette.testclient import TestClient

from backend.api.app import app
from backend.api.schemas.topology import (
    TopologyErrorFeature,
    TopologyErrorFeatureCollection,
    TopologyErrorProperties,
    build_topology_error_feature_collection,
)
from backend.db.session import get_async_session
from backend.models.layer import LayerStatus, RawCadastralLayer
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.topology.recorder import project_metric_geometry_to_wgs84


def _make_layer(layer_id: uuid.UUID) -> RawCadastralLayer:
    layer = RawCadastralLayer(
        name="Test Survey Layer",
        filename="survey.geojson",
        file_format="GEOJSON",
        storage_path="/tmp/survey.geojson",
        source_crs="EPSG:32643",
        target_crs="EPSG:4326",
        status=LayerStatus.COMPLETED.value,
    )
    layer.id = layer_id
    return layer


def _make_overlap_error(
    layer_id: uuid.UUID,
    error_id: uuid.UUID,
    parcel_a_id: uuid.UUID,
    parcel_b_id: uuid.UUID,
    *,
    severity: TopologySeverity = TopologySeverity.CRITICAL_DISPUTE,
    pct_a: float = 0.8,
    pct_b: float = 14.5,
    area_sqm: float = 29.0,
    is_resolved: bool = False,
) -> TopologicalError:
    utm_mp = MultiPolygon(
        [
            Polygon(
                [
                    (720_000.0, 3_170_000.0),
                    (720_010.0, 3_170_000.0),
                    (720_010.0, 3_170_002.9),
                    (720_000.0, 3_170_002.9),
                    (720_000.0, 3_170_000.0),
                ]
            )
        ]
    )
    wgs84_mp = project_metric_geometry_to_wgs84(utm_mp)
    err = TopologicalError(
        layer_id=layer_id,
        primary_parcel_id=parcel_a_id,
        secondary_parcel_id=parcel_b_id,
        error_type=TopologyErrorType.OVERLAP.value,
        severity=severity.value,
        description="Critical boundary overlap",
        overlap_percentage_primary=pct_a,
        overlap_percentage_secondary=pct_b,
        affected_area_sqm=area_sqm,
        error_geometry_wgs84=from_shape(wgs84_mp, srid=4326),
        error_geometry_projected=from_shape(utm_mp, srid=32643),
        is_resolved=is_resolved,
    )
    err.id = error_id
    return err


def _make_dangling_node_error(
    layer_id: uuid.UUID,
    error_id: uuid.UUID,
    parcel_id: uuid.UUID,
    *,
    is_resolved: bool = False,
) -> TopologicalError:
    utm_pt = Point(720_050.0, 3_170_050.0)
    wgs84_pt = project_metric_geometry_to_wgs84(utm_pt)
    err = TopologicalError(
        layer_id=layer_id,
        primary_parcel_id=parcel_id,
        secondary_parcel_id=None,
        error_type=TopologyErrorType.DANGLING_NODE.value,
        severity=TopologySeverity.AUTO_RESOLVABLE.value,
        description="Unsnapped boundary vertex (0.12 m)",
        affected_area_sqm=0.0,
        error_geometry_wgs84=from_shape(wgs84_pt, srid=4326),
        error_geometry_projected=from_shape(utm_pt, srid=32643),
        is_resolved=is_resolved,
    )
    err.id = error_id
    return err


def _mock_session_with_layer_and_errors(
    layer: RawCadastralLayer | None,
    errors: list[TopologicalError],
) -> AsyncMock:
    session = AsyncMock()

    layer_result = MagicMock()
    layer_result.scalar_one_or_none.return_value = layer

    errors_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = errors
    errors_result.scalars.return_value = scalars_mock

    session.execute.side_effect = [layer_result, errors_result]
    return session


class TestTopologyErrorFeatureCollectionSchema:
    """Unit tests for `build_topology_error_feature_collection` RFC 7946 serialization and WGS 84 bbox."""

    def test_builds_valid_rfc7946_feature_collection_with_wgs84_bbox_and_counts(self):
        layer_id = uuid.uuid4()
        err_overlap = _make_overlap_error(
            layer_id,
            uuid.UUID("00000000-0000-0000-0000-000000000101"),
            uuid.UUID("00000000-0000-0000-0000-000000000001"),
            uuid.UUID("00000000-0000-0000-0000-000000000002"),
            severity=TopologySeverity.CRITICAL_DISPUTE,
            is_resolved=False,
        )
        err_dangle = _make_dangling_node_error(
            layer_id,
            uuid.UUID("00000000-0000-0000-0000-000000000102"),
            uuid.UUID("00000000-0000-0000-0000-000000000003"),
            is_resolved=True,
        )

        fc = build_topology_error_feature_collection(layer_id, [err_overlap, err_dangle])

        assert isinstance(fc, TopologyErrorFeatureCollection)
        assert fc.type == "FeatureCollection"
        assert fc.layer_id == str(layer_id)
        assert fc.total_count == 2
        assert fc.critical_dispute_count == 1
        assert fc.auto_resolvable_count == 1
        assert fc.resolved_count == 1
        assert fc.unresolved_count == 1
        assert fc.counts_by_type == {"OVERLAP": 1, "DANGLING_NODE": 1}

        assert fc.bbox is not None
        assert len(fc.bbox) == 4
        min_lon, min_lat, max_lon, max_lat = fc.bbox
        assert -180.0 <= min_lon < max_lon <= 180.0
        assert -90.0 <= min_lat < max_lat <= 90.0

        assert len(fc.features) == 2
        f0 = fc.features[0]
        assert isinstance(f0, TopologyErrorFeature)
        assert f0.type == "Feature"
        assert f0.geometry["type"] == "MultiPolygon"
        assert isinstance(f0.properties, TopologyErrorProperties)
        assert f0.properties.error_type == "OVERLAP"
        assert f0.properties.severity == "CRITICAL_DISPUTE"
        assert f0.properties.overlap_percentage_primary == pytest.approx(0.8)
        assert f0.properties.overlap_percentage_secondary == pytest.approx(14.5)

    def test_empty_error_list_returns_empty_feature_collection_with_null_bbox(self):
        layer_id = uuid.uuid4()
        fc = build_topology_error_feature_collection(layer_id, [])
        assert fc.type == "FeatureCollection"
        assert fc.layer_id == str(layer_id)
        assert fc.total_count == 0
        assert fc.features == []
        assert fc.bbox is None
        assert fc.counts_by_type == {}


class TestTopologyErrorsAPIEndpoint:
    """Integration tests for `GET /api/v1/layers/{layer_id}/topology-errors`."""

    def teardown_method(self):
        app.dependency_overrides.clear()

    def test_get_layer_topology_errors_returns_200_feature_collection(self):
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)
        err_1 = _make_overlap_error(
            layer_id,
            uuid.UUID("00000000-0000-0000-0000-000000000201"),
            uuid.UUID("00000000-0000-0000-0000-000000000001"),
            uuid.UUID("00000000-0000-0000-0000-000000000002"),
        )
        err_2 = _make_dangling_node_error(
            layer_id,
            uuid.UUID("00000000-0000-0000-0000-000000000202"),
            uuid.UUID("00000000-0000-0000-0000-000000000003"),
        )

        mock_session = _mock_session_with_layer_and_errors(layer, [err_1, err_2])
        app.dependency_overrides[get_async_session] = lambda: mock_session

        with TestClient(app) as client:
            resp = client.get(f"/api/v1/layers/{layer_id}/topology-errors")

        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "FeatureCollection"
        assert body["layer_id"] == str(layer_id)
        assert body["total_count"] == 2
        assert body["critical_dispute_count"] == 1
        assert body["auto_resolvable_count"] == 1
        assert len(body["features"]) == 2
        assert body["bbox"] is not None and len(body["bbox"]) == 4

    def test_get_layer_topology_errors_supports_query_filters(self):
        layer_id = uuid.uuid4()
        layer = _make_layer(layer_id)
        err_1 = _make_overlap_error(
            layer_id,
            uuid.UUID("00000000-0000-0000-0000-000000000201"),
            uuid.UUID("00000000-0000-0000-0000-000000000001"),
            uuid.UUID("00000000-0000-0000-0000-000000000002"),
        )

        mock_session = _mock_session_with_layer_and_errors(layer, [err_1])
        app.dependency_overrides[get_async_session] = lambda: mock_session

        with TestClient(app) as client:
            resp = client.get(
                f"/api/v1/layers/{layer_id}/topology-errors",
                params={
                    "severity": "CRITICAL_DISPUTE",
                    "error_type": "OVERLAP",
                    "is_resolved": "false",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["features"][0]["properties"]["severity"] == "CRITICAL_DISPUTE"
        assert body["features"][0]["properties"]["error_type"] == "OVERLAP"

    def test_get_layer_topology_errors_returns_404_when_layer_not_found(self):
        layer_id = uuid.uuid4()
        mock_session = _mock_session_with_layer_and_errors(None, [])
        app.dependency_overrides[get_async_session] = lambda: mock_session

        with TestClient(app) as client:
            resp = client.get(f"/api/v1/layers/{layer_id}/topology-errors")

        assert resp.status_code == 404
        assert str(layer_id) in resp.json()["detail"]

    def test_get_layer_topology_errors_returns_422_on_invalid_filter_enum(self):
        layer_id = uuid.uuid4()
        mock_session = _mock_session_with_layer_and_errors(_make_layer(layer_id), [])
        app.dependency_overrides[get_async_session] = lambda: mock_session

        with TestClient(app) as client:
            resp = client.get(
                f"/api/v1/layers/{layer_id}/topology-errors",
                params={"severity": "INVALID_SEVERITY"},
            )

        assert resp.status_code == 422

    def test_get_layer_topology_errors_returns_503_when_db_unavailable(self):
        layer_id = uuid.uuid4()
        app.dependency_overrides[get_async_session] = lambda: None

        with TestClient(app) as client:
            resp = client.get(f"/api/v1/layers/{layer_id}/topology-errors")

        assert resp.status_code == 503
