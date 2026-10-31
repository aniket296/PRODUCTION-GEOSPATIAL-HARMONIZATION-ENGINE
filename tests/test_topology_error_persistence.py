"""Tests for persistence of detected topology errors and conflict recording service (Step 84/150).

Validates:
1. Construction of `TopologicalError` ORM records across all anomaly categories (`OVERLAP`, `SLIVER`, `GAP`,
   `DUPLICATE_VERTEX`, `DANGLING_NODE`) with dual-CRS geometries (`error_geometry_projected` at SRID 32643 and
   `error_geometry_wgs84` at SRID 4326).
2. Independent overlap percentages (`overlap_percentage_primary`, `overlap_percentage_secondary`) and deterministic
   `AUTO_RESOLVABLE` vs `CRITICAL_DISPUTE` severity assignment.
3. Automatic dispute locking (`is_disputed=True`, `dispute_locked=True`, `resolution_status="DISPUTED"`) on
   `HarmonizedParcel` instances involved in a `CRITICAL_DISPUTE` overlap, and `is_sliver=True` flagging on sliver
   parcels.
4. Asynchronous database session persistence (`persist_layer_conflicts`), including batch `add_all`, `flush`,
   optional replacement of existing unresolved errors for the layer, and summary aggregation
   (`TopologyRecordingSummary`).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.topology.recorder import (
    TopologyConflictRecorder,
    TopologyRecordingSummary,
    project_metric_geometry_to_wgs84,
)


def _make_parcel(
    layer_id: uuid.UUID,
    parcel_id: uuid.UUID,
    identifier: str,
    poly_utm: Polygon,
    *,
    dispute_locked: bool = False,
) -> HarmonizedParcel:
    mp_utm = MultiPolygon([poly_utm])
    geom_wgs84 = project_metric_geometry_to_wgs84(mp_utm)
    return HarmonizedParcel(
        id=parcel_id,
        layer_id=layer_id,
        parcel_identifier=identifier,
        geometry_wgs84=from_shape(geom_wgs84, srid=4326),
        geometry_projected=from_shape(mp_utm, srid=32643),
        metric_area_sqm=float(poly_utm.area),
        metric_perimeter_m=float(poly_utm.length),
        dispute_locked=dispute_locked,
        is_disputed=dispute_locked,
    )


class TestDualCrsGeometryConversion:
    """Verify projected UTM 43N (EPSG:32643) to WGS 84 (EPSG:4326) geometry conversion for error persistence."""

    def test_project_metric_geometry_to_wgs84_returns_valid_lon_lat_bounds(self):
        # UTM Zone 43N coordinates around New Delhi / northern India (Easting ~ 720,000m, Northing ~ 3,170,000m)
        utm_poly = Polygon(
            [
                (720_000.0, 3_170_000.0),
                (720_100.0, 3_170_000.0),
                (720_100.0, 3_170_100.0),
                (720_000.0, 3_170_100.0),
                (720_000.0, 3_170_000.0),
            ]
        )
        wgs84_geom = project_metric_geometry_to_wgs84(utm_poly)
        minx, miny, maxx, maxy = wgs84_geom.bounds

        assert 70.0 <= minx < maxx <= 80.0
        assert 25.0 <= miny < maxy <= 32.0
        assert wgs84_geom.is_valid


class TestTopologyConflictDetectionAndModelCreation:
    """Test `TopologyConflictRecorder.build_layer_errors` across micro-overlap, critical dispute, sliver, gap, and vertex anomalies."""

    def test_micro_overlap_creates_auto_resolvable_error_without_locking_parcels(self):
        layer_id = uuid.uuid4()
        id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")

        # Two 100m x 100m (10,000 m²) parcels overlapping on a 1m x 100m (100 m² = 1.0% <= 2.0%) strip
        p_a = _make_parcel(
            layer_id,
            id_a,
            "KHASRA-01",
            Polygon([(500_000.0, 3_000_000.0), (500_100.0, 3_000_000.0), (500_100.0, 3_000_100.0), (500_000.0, 3_000_100.0)]),
        )
        p_b = _make_parcel(
            layer_id,
            id_b,
            "KHASRA-02",
            Polygon([(500_099.0, 3_000_000.0), (500_199.0, 3_000_000.0), (500_199.0, 3_000_100.0), (500_099.0, 3_000_100.0)]),
        )

        recorder = TopologyConflictRecorder()
        errors = recorder.build_layer_errors(layer_id, [p_a, p_b])
        overlap_errors = [e for e in errors if e.error_type == TopologyErrorType.OVERLAP.value]

        assert len(overlap_errors) == 1
        err = overlap_errors[0]
        assert err.layer_id == layer_id
        assert err.primary_parcel_id == id_a
        assert err.secondary_parcel_id == id_b
        assert err.severity == TopologySeverity.AUTO_RESOLVABLE.value
        assert err.affected_area_sqm == pytest.approx(100.0, abs=1e-6)
        assert err.overlap_percentage_primary == pytest.approx(1.0, abs=1e-6)
        assert err.overlap_percentage_secondary == pytest.approx(1.0, abs=1e-6)
        assert isinstance(err.error_geometry_projected, WKBElement)
        assert isinstance(err.error_geometry_wgs84, WKBElement)
        assert err.error_geometry_projected.srid == 32643
        assert err.error_geometry_wgs84.srid == 4326
        assert p_a.dispute_locked is False
        assert p_b.dispute_locked is False

    def test_critical_overlap_locks_both_parcels_and_records_critical_dispute(self):
        layer_id = uuid.uuid4()
        id_a = uuid.UUID("00000000-0000-0000-0000-000000000010")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000020")

        # Parcel A: 100m x 100m (10,000 m²); Parcel B: 10m x 20m (200 m²) overlapping by 2m x 20m = 40 m²
        # pct_a = 0.4% (<= 2%), pct_b = 20.0% (> 2%) -> CRITICAL_DISPUTE
        p_a = _make_parcel(
            layer_id,
            id_a,
            "ESTATE-A",
            Polygon([(500_000.0, 3_000_000.0), (500_100.0, 3_000_000.0), (500_100.0, 3_000_100.0), (500_000.0, 3_000_100.0)]),
        )
        p_b = _make_parcel(
            layer_id,
            id_b,
            "PLOT-B",
            Polygon([(500_098.0, 3_000_000.0), (500_108.0, 3_000_000.0), (500_108.0, 3_000_020.0), (500_098.0, 3_000_020.0)]),
        )

        recorder = TopologyConflictRecorder()
        errors = recorder.build_layer_errors(layer_id, [p_a, p_b])
        overlap_errors = [e for e in errors if e.error_type == TopologyErrorType.OVERLAP.value]

        assert len(overlap_errors) == 1
        err = overlap_errors[0]
        assert err.severity == TopologySeverity.CRITICAL_DISPUTE.value
        assert err.overlap_percentage_primary == pytest.approx(0.4, abs=1e-6)
        assert err.overlap_percentage_secondary == pytest.approx(20.0, abs=1e-6)
        assert p_a.is_disputed is True and p_a.dispute_locked is True
        assert p_b.is_disputed is True and p_b.dispute_locked is True
        assert p_a.resolution_status == ParcelResolutionStatus.DISPUTED.value
        assert p_b.resolution_status == ParcelResolutionStatus.DISPUTED.value

    def test_detects_sliver_gap_and_duplicate_vertex_errors_in_single_pass(self):
        layer_id = uuid.uuid4()
        id_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
        id_sliver = uuid.UUID("00000000-0000-0000-0000-000000000003")

        # Parcel 1 has a duplicate consecutive vertex at (500_010, 3_000_000)
        poly_1 = Polygon(
            [
                (500_000.0, 3_000_000.0),
                (500_010.0, 3_000_000.0),
                (500_010.0, 3_000_000.0),
                (500_010.0, 3_000_010.0),
                (500_000.0, 3_000_010.0),
                (500_000.0, 3_000_000.0),
            ]
        )
        # Parcel 2 is separated from Parcel 1 by a 0.3m parallel boundary gap (area = 3.0 m²)
        poly_2 = Polygon(
            [
                (500_010.3, 3_000_000.0),
                (500_020.3, 3_000_000.0),
                (500_020.3, 3_000_010.0),
                (500_010.3, 3_000_010.0),
                (500_010.3, 3_000_000.0),
            ]
        )
        # Parcel 3 is a 0.1m x 50m sliver far away (area = 5.0 m² < 10.0, PP ~ 0.00626 < 0.05)
        poly_sliver = Polygon(
            [
                (500_500.0, 3_000_000.0),
                (500_550.0, 3_000_000.0),
                (500_550.0, 3_000_000.1),
                (500_500.0, 3_000_000.1),
                (500_500.0, 3_000_000.0),
            ]
        )

        p1 = _make_parcel(layer_id, id_1, "P-1", poly_1)
        p2 = _make_parcel(layer_id, id_2, "P-2", poly_2)
        p_sliver = _make_parcel(layer_id, id_sliver, "P-SLIVER", poly_sliver)

        recorder = TopologyConflictRecorder()
        errors = recorder.build_layer_errors(layer_id, [p1, p2, p_sliver])
        by_type = {e.error_type: e for e in errors}

        assert TopologyErrorType.SLIVER.value in by_type
        assert TopologyErrorType.GAP.value in by_type
        assert TopologyErrorType.DUPLICATE_VERTEX.value in by_type
        assert p_sliver.is_sliver is True

        sliver_err = by_type[TopologyErrorType.SLIVER.value]
        assert sliver_err.primary_parcel_id == id_sliver
        assert sliver_err.affected_area_sqm == pytest.approx(5.0, abs=1e-6)
        assert sliver_err.severity == TopologySeverity.AUTO_RESOLVABLE.value

        gap_err = by_type[TopologyErrorType.GAP.value]
        assert gap_err.primary_parcel_id == id_1
        assert gap_err.secondary_parcel_id == id_2
        assert gap_err.affected_area_sqm == pytest.approx(3.0, rel=0.02)

        dup_err = by_type[TopologyErrorType.DUPLICATE_VERTEX.value]
        assert dup_err.primary_parcel_id == id_1
        assert to_shape(dup_err.error_geometry_projected).geom_type == "Point"


class TestAsyncSessionPersistence:
    """Verify `TopologyConflictRecorder.persist_layer_conflicts` persists records via AsyncSession."""

    @pytest.mark.asyncio
    async def test_persist_layer_conflicts_adds_errors_flushes_and_returns_summary(self):
        layer_id = uuid.uuid4()
        id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")

        p_a = _make_parcel(
            layer_id,
            id_a,
            "A",
            Polygon([(500_000.0, 3_000_000.0), (500_100.0, 3_000_000.0), (500_100.0, 3_000_100.0), (500_000.0, 3_000_100.0)]),
        )
        p_b = _make_parcel(
            layer_id,
            id_b,
            "B",
            Polygon([(500_090.0, 3_000_000.0), (500_190.0, 3_000_000.0), (500_190.0, 3_000_100.0), (500_090.0, 3_000_100.0)]),
        )

        session = AsyncMock()
        session.add_all = MagicMock()

        recorder = TopologyConflictRecorder()
        summary = await recorder.persist_layer_conflicts(
            session=session,
            layer_id=layer_id,
            parcels=[p_a, p_b],
            replace_unresolved=True,
        )

        assert isinstance(summary, TopologyRecordingSummary)
        assert summary.layer_id == layer_id
        assert summary.total_errors == 1
        assert summary.critical_dispute_count == 1
        assert summary.auto_resolvable_count == 0
        assert summary.counts_by_type[TopologyErrorType.OVERLAP.value] == 1

        session.execute.assert_awaited_once()
        session.add_all.assert_called_once()
        persisted_models = session.add_all.call_args[0][0]
        assert len(persisted_models) == 1
        assert isinstance(persisted_models[0], TopologicalError)
        session.flush.assert_awaited_once()
