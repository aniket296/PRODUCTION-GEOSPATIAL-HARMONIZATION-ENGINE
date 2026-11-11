"""Tests for atomic healing pipeline orchestration and transaction rollback on invalid geometry (Step 100/150).

Directive Sections 20-25:
- The healing orchestrator runs the four deterministic stages in order:
  1. Critical-dispute detection and locking (`apply_dispute_locks`)
  2. Sliver assignment and union (`heal_layer_slivers`)
  3. Micro-overlap centerline splitting (`heal_layer_micro_overlaps`)
  4. Metric boundary snapping (`snap_layer_boundaries`) and 1 mm grid normalization (`normalize_layer_grid`)
- Atomicity guarantee: if any stage produces an invalid geometry, rejects a parcel under `strict_validity=True`,
  violates area conservation, or modifies a dispute-locked parcel, the entire batch is rolled back so every
  `HarmonizedParcel` record (and the database session when running via `execute_healing_transaction`) returns to its
  exact pre-healing state (`geometry_projected`, `geometry_wgs84`, metrics, flags, `resolution_status`, `attributes`),
  and `HealingTransactionRollbackError` is raised.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.healing.orchestrator import (
    HealingPipelineResult,
    HealingTransactionRollbackError,
    ParcelStateSnapshot,
    capture_parcel_snapshots,
    execute_healing_transaction,
    execute_layer_healing_pipeline,
    restore_parcel_snapshots,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84

ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_C = uuid.UUID("00000000-0000-0000-0000-000000000003")
ID_S = uuid.UUID("00000000-0000-0000-0000-000000000099")

DX, DY = 500_000.0, 3_000_000.0


def _rect(x0: float, y0: float, x1: float, y1: float, dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return Polygon(
        [(x0 + dx, y0 + dy), (x1 + dx, y0 + dy), (x1 + dx, y1 + dy), (x0 + dx, y1 + dy), (x0 + dx, y0 + dy)]
    )


def _orm(
    pid: uuid.UUID,
    ident: str,
    poly: Polygon,
    *,
    locked: bool = False,
    sliver: bool = False,
    attributes: dict | None = None,
) -> HarmonizedParcel:
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=pid,
        layer_id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        parcel_identifier=ident,
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        dispute_locked=locked,
        is_disputed=locked,
        is_sliver=sliver,
        attributes=attributes,
    )


class TestParcelSnapshotAndRestore:
    def test_snapshot_and_restore_roundtrip_preserves_exact_wkb_and_metadata(self):
        p = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY), attributes={"k": "v"})
        orig_proj = bytes(p.geometry_projected.data)
        orig_wgs = bytes(p.geometry_wgs84.data)

        snapshots = capture_parcel_snapshots([p])
        assert isinstance(snapshots[ID_A], ParcelStateSnapshot)

        # Mutate parcel heavily
        mutated = MultiPolygon([_rect(0.0, 0.0, 50.0, 50.0, DX, DY)])
        p.geometry_projected = from_shape(mutated, srid=32643)
        p.recalculate_metrics(mutated)
        p.lock_dispute("temp")

        restore_parcel_snapshots([p], snapshots)

        assert bytes(p.geometry_projected.data) == orig_proj
        assert bytes(p.geometry_wgs84.data) == orig_wgs
        assert p.metric_area_sqm == pytest.approx(10_000.0, abs=1e-6)
        assert p.dispute_locked is False
        assert p.is_disputed is False
        assert p.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        assert p.attributes == {"k": "v"}


class TestHealingPipelineSuccess:
    def test_orchestrates_dispute_lock_sliver_union_micro_overlap_and_grid_snap(self):
        # A [0, 100] x [0, 100] and B [99, 199] x [0, 100] have a 1% micro-overlap (resolved at x = 99.5)
        # S [-0.1, 0.0] x [0, 40] is a 4 m² sliver adjacent to A's west edge (merged into A)
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_b = _orm(ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, DX, DY))
        p_s = _orm(ID_S, "S", _rect(-0.1, 0.0, 0.0, 40.0, DX, DY), sliver=True)

        result = execute_layer_healing_pipeline([p_b, p_s, p_a])

        assert isinstance(result, HealingPipelineResult)
        assert len(result.sliver_result.assignments) == 1
        assert result.sliver_result.assignments[0].neighbor_parcel_id == ID_A
        assert len(result.micro_overlap_result.resolved_pairs) == 1
        assert set(result.auto_resolved_parcel_ids) == {ID_A, ID_B, ID_S}
        assert result.disputed_parcel_ids == ()
        assert p_a.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert p_b.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert p_s.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert to_shape(p_a.geometry_projected).is_valid
        assert to_shape(p_b.geometry_projected).is_valid


class TestHealingTransactionRollbackOnFailure:
    def test_rolls_back_all_parcels_when_grid_normalization_encounters_collapsing_polygon(self):
        # p_a and p_b would normally heal their 1% micro-overlap, and p_s would merge into p_a,
        # but p_c is a sub-millimeter degenerate polygon (0.0002 m x 0.0002 m) that collapses on the 1 mm grid.
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY), attributes={"owner": "Rao"})
        p_b = _orm(ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, DX, DY))
        p_s = _orm(ID_S, "S", _rect(-0.1, 0.0, 0.0, 40.0, DX, DY), sliver=True)
        p_c = _orm(ID_C, "C", _rect(500.0001, 500.0001, 500.0003, 500.0003, DX, DY))

        before_a = (bytes(p_a.geometry_projected.data), bytes(p_a.geometry_wgs84.data), p_a.metric_area_sqm)
        before_b = (bytes(p_b.geometry_projected.data), bytes(p_b.geometry_wgs84.data), p_b.metric_area_sqm)
        before_s = (bytes(p_s.geometry_projected.data), p_s.attributes)

        with pytest.raises(HealingTransactionRollbackError, match=str(ID_C)):
            execute_layer_healing_pipeline([p_a, p_b, p_s, p_c])

        # Every parcel must be restored to its exact pre-pipeline state
        assert (bytes(p_a.geometry_projected.data), bytes(p_a.geometry_wgs84.data), p_a.metric_area_sqm) == before_a
        assert (bytes(p_b.geometry_projected.data), bytes(p_b.geometry_wgs84.data), p_b.metric_area_sqm) == before_b
        assert (bytes(p_s.geometry_projected.data), p_s.attributes) == before_s
        for parcel in (p_a, p_b, p_s, p_c):
            assert parcel.resolution_status == ParcelResolutionStatus.ORIGINAL.value
            assert parcel.dispute_locked is False

    def test_rolls_back_when_boundary_snap_produces_self_intersecting_geometry(self):
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        bait_poly = Polygon(
            [
                (100.01 + DX, 99.73 + DY),
                (100.24 + DX, 100.46 + DY),
                (100.48 + DX, 99.89 + DY),
                (100.13 + DX, 99.93 + DY),
                (100.01 + DX, 99.73 + DY),
            ]
        )
        p_c = _orm(ID_C, "C", bait_poly)
        before_a = bytes(p_a.geometry_projected.data)
        before_c = bytes(p_c.geometry_projected.data)

        with pytest.raises(HealingTransactionRollbackError, match=str(ID_C)):
            execute_layer_healing_pipeline([p_a, p_c])

        assert bytes(p_a.geometry_projected.data) == before_a
        assert bytes(p_c.geometry_projected.data) == before_c
        assert p_a.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        assert p_c.resolution_status == ParcelResolutionStatus.ORIGINAL.value

    def test_async_transaction_flushes_on_success_and_rolls_back_session_on_failure(self):
        def _drive(coro):
            try:
                while True:
                    coro.send(None)
            except StopIteration as stop:
                return stop.value

        session = AsyncMock()
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_b = _orm(ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, DX, DY))

        res = _drive(execute_healing_transaction(session, [p_a, p_b]))
        assert len(res.micro_overlap_result.resolved_pairs) == 1
        session.flush.assert_awaited_once()
        session.rollback.assert_not_awaited()

        # Now trigger rollback with a collapsing sub-grid parcel
        session_fail = AsyncMock()
        p_a2 = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_c2 = _orm(ID_C, "C", _rect(500.0001, 500.0001, 500.0003, 500.0003, DX, DY))

        with pytest.raises(HealingTransactionRollbackError):
            _drive(execute_healing_transaction(session_fail, [p_a2, p_c2]))

        session_fail.rollback.assert_awaited_once()
        session_fail.flush.assert_not_awaited()
        assert p_a2.resolution_status == ParcelResolutionStatus.ORIGINAL.value

    def test_rollback_error_is_a_value_error(self):
        assert issubclass(HealingTransactionRollbackError, ValueError)
