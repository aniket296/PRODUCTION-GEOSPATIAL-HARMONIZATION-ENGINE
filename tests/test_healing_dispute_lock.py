"""Tests for critical-dispute locking and geometry preservation (Step 94/150).

Directive Sections 15, 20-23, 25:
- Any pairwise overlap whose independent overlap percentage exceeds `max_overlap_percent_threshold` (default 2.0%)
  on EITHER parcel is a `CRITICAL_DISPUTE`. Both parcels are dispute-locked: `is_disputed = True`,
  `dispute_locked = True`, `resolution_status = DISPUTED`, with an auditable `dispute_reason` and the sorted list of
  conflicting parcel IDs in `attributes["disputed_with_parcel_ids"]`.
- Dispute locking NEVER modifies geometry (projected EPSG:32643 and WGS 84 EPSG:4326 WKB are byte-identical).
- Locked parcels are excluded from automated healing; subsequent sliver union and micro-overlap healing must leave
  them untouched, and `mark_auto_resolved()` on a locked parcel must raise.
- `snapshot_locked_geometries` + `assert_locked_geometries_preserved` provide a post-healing guard that raises
  `DisputedGeometryModifiedError` if any locked parcel geometry changed.

Expected API in `backend.healing.dispute_protection`:
- `DisputeCandidateInput(parcel_id, geometry, dispute_locked=False)`
- `CriticalDisputePair(parcel_a_id, parcel_b_id, intersection_area_sqm, overlap_percentage_a, overlap_percentage_b)`
- `DisputeProtectionResult(critical_pairs, locked_parcel_ids, newly_locked_parcel_ids)`
- `detect_critical_disputes(candidates, *, threshold_percent=None) -> DisputeProtectionResult`
- `apply_dispute_locks(parcels, *, threshold_percent=None) -> DisputeProtectionResult`
- `filter_healable_parcels(parcels) -> tuple[HarmonizedParcel, ...]`
- `snapshot_locked_geometries(parcels) -> dict[uuid.UUID, tuple[bytes, bytes]]`
- `assert_locked_geometries_preserved(parcels, snapshot) -> None`
- `DisputedGeometryModifiedError(ValueError)`
"""

from __future__ import annotations

import uuid

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.healing.dispute_protection import (
    CriticalDisputePair,
    DisputeCandidateInput,
    DisputedGeometryModifiedError,
    DisputeProtectionResult,
    apply_dispute_locks,
    assert_locked_geometries_preserved,
    detect_critical_disputes,
    filter_healable_parcels,
    snapshot_locked_geometries,
)
from backend.healing.micro_overlap import heal_layer_micro_overlaps
from backend.healing.sliver_union import heal_layer_slivers
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84

ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_C = uuid.UUID("00000000-0000-0000-0000-000000000003")
ID_D = uuid.UUID("00000000-0000-0000-0000-000000000004")

DX, DY = 500_000.0, 3_000_000.0


def _rect(x0: float, y0: float, x1: float, y1: float, dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return Polygon(
        [(x0 + dx, y0 + dy), (x1 + dx, y0 + dy), (x1 + dx, y1 + dy), (x0 + dx, y1 + dy), (x0 + dx, y0 + dy)]
    )


def _cand(pid: uuid.UUID, poly: Polygon, *, locked: bool = False) -> DisputeCandidateInput:
    return DisputeCandidateInput(parcel_id=pid, geometry=poly, dispute_locked=locked)


def _orm(pid: uuid.UUID, ident: str, poly: Polygon, *, locked: bool = False, sliver: bool = False) -> HarmonizedParcel:
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
    )


def _wkb(p: HarmonizedParcel) -> tuple[bytes, bytes]:
    return bytes(p.geometry_projected.data), bytes(p.geometry_wgs84.data)


# A [0,100]² (10,000 m²) and B [90,190]x[0,100] overlap 1,000 m² = 10% -> CRITICAL_DISPUTE.
# C [0,80]x[99.5,199.5] overlaps A by 40 m² (0.4% of A, 0.5% of C) -> micro-overlap, but A will be locked.
def _poly_a(dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return _rect(0.0, 0.0, 100.0, 100.0, dx, dy)


def _poly_b(dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return _rect(90.0, 0.0, 190.0, 100.0, dx, dy)


def _poly_c(dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return _rect(0.0, 99.5, 80.0, 199.5, dx, dy)


class TestDetectCriticalDisputes:
    def test_overlap_above_threshold_is_critical_and_locks_both_parcels(self):
        result = detect_critical_disputes([_cand(ID_B, _poly_b()), _cand(ID_A, _poly_a())])

        assert isinstance(result, DisputeProtectionResult)
        assert len(result.critical_pairs) == 1
        pair = result.critical_pairs[0]
        assert isinstance(pair, CriticalDisputePair)
        assert (pair.parcel_a_id, pair.parcel_b_id) == (ID_A, ID_B)
        assert pair.intersection_area_sqm == pytest.approx(1_000.0, abs=1e-6)
        assert pair.overlap_percentage_a == pytest.approx(10.0, abs=1e-6)
        assert pair.overlap_percentage_b == pytest.approx(10.0, abs=1e-6)
        assert result.locked_parcel_ids == (ID_A, ID_B)
        assert result.newly_locked_parcel_ids == (ID_A, ID_B)

    def test_micro_overlap_within_threshold_is_not_locked(self):
        result = detect_critical_disputes([_cand(ID_A, _poly_a()), _cand(ID_C, _poly_c())])
        assert result.critical_pairs == ()
        assert result.locked_parcel_ids == ()
        assert result.newly_locked_parcel_ids == ()

    def test_asymmetric_overlap_exceeding_threshold_on_small_parcel_is_critical(self):
        # 10 m² overlap: 0.1% of A but 10% of the 100 m² parcel D.
        small = _rect(99.0, 0.0, 109.0, 10.0)
        result = detect_critical_disputes([_cand(ID_A, _poly_a()), _cand(ID_D, small)])

        assert len(result.critical_pairs) == 1
        pair = result.critical_pairs[0]
        assert pair.overlap_percentage_a == pytest.approx(0.1, abs=1e-6)
        assert pair.overlap_percentage_b == pytest.approx(10.0, abs=1e-6)
        assert result.locked_parcel_ids == (ID_A, ID_D)

    def test_exactly_at_threshold_is_not_critical(self):
        # 200 m² overlap on 10,000 m² parcels = exactly 2.0%.
        west = _rect(0.0, 0.0, 100.0, 100.0)
        east = _rect(98.0, 0.0, 198.0, 100.0)
        result = detect_critical_disputes([_cand(ID_A, west), _cand(ID_B, east)])
        assert result.critical_pairs == ()

    def test_custom_threshold_is_respected(self):
        result = detect_critical_disputes(
            [_cand(ID_A, _poly_a()), _cand(ID_C, _poly_c())], threshold_percent=0.1
        )
        assert [(p.parcel_a_id, p.parcel_b_id) for p in result.critical_pairs] == [(ID_A, ID_C)]

    def test_already_locked_parcel_is_reported_but_not_newly_locked(self):
        far = _rect(1_000.0, 1_000.0, 1_100.0, 1_100.0)
        result = detect_critical_disputes([_cand(ID_A, _poly_a()), _cand(ID_D, far, locked=True)])
        assert result.critical_pairs == ()
        assert result.locked_parcel_ids == (ID_D,)
        assert result.newly_locked_parcel_ids == ()

    def test_detection_is_independent_of_input_order(self):
        cands = [_cand(ID_A, _poly_a()), _cand(ID_B, _poly_b()), _cand(ID_C, _poly_c())]
        baseline = detect_critical_disputes(cands)
        assert detect_critical_disputes(cands[::-1]) == baseline
        assert detect_critical_disputes([cands[1], cands[2], cands[0]]) == baseline

    def test_invalid_threshold_raises_value_error(self):
        with pytest.raises(ValueError, match="threshold_percent"):
            detect_critical_disputes([_cand(ID_A, _poly_a())], threshold_percent=-1.0)


class TestApplyDisputeLocksOnParcels:
    def test_locks_parcels_without_modifying_geometry(self):
        a, b = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY))
        before_a, before_b = _wkb(a), _wkb(b)

        result = apply_dispute_locks([b, a])

        assert result.newly_locked_parcel_ids == (ID_A, ID_B)
        for parcel, before in ((a, before_a), (b, before_b)):
            assert _wkb(parcel) == before
            assert parcel.dispute_locked is True
            assert parcel.is_disputed is True
            assert parcel.resolution_status == ParcelResolutionStatus.DISPUTED.value

    def test_dispute_reason_and_conflicting_ids_are_recorded(self):
        a, b = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY))

        apply_dispute_locks([a, b])

        assert "CRITICAL_DISPUTE" in a.attributes["dispute_reason"]
        assert str(ID_B) in a.attributes["dispute_reason"]
        assert a.attributes["disputed_with_parcel_ids"] == [str(ID_B)]
        assert b.attributes["disputed_with_parcel_ids"] == [str(ID_A)]

    def test_parcel_in_multiple_disputes_lists_all_conflicts_sorted(self):
        a = _orm(ID_A, "A", _poly_a(DX, DY))
        b = _orm(ID_B, "B", _poly_b(DX, DY))
        d = _orm(ID_D, "D", _rect(-10.0, 0.0, 10.0, 100.0, DX, DY))  # overlaps A by 1,000 m² (10%)

        apply_dispute_locks([d, b, a])

        assert a.attributes["disputed_with_parcel_ids"] == [str(ID_B), str(ID_D)]

    def test_existing_attributes_are_preserved(self):
        a, b = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY))
        a.attributes = {"survey_no": "17/2"}

        apply_dispute_locks([a, b])

        assert a.attributes["survey_no"] == "17/2"
        assert "dispute_reason" in a.attributes

    def test_parcels_without_critical_overlap_are_untouched(self):
        a, c = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_C, "C", _poly_c(DX, DY))

        result = apply_dispute_locks([a, c])

        assert result.critical_pairs == ()
        for parcel in (a, c):
            assert parcel.dispute_locked is False
            assert parcel.resolution_status == ParcelResolutionStatus.ORIGINAL.value
            assert parcel.attributes is None

    def test_locked_parcel_cannot_be_marked_auto_resolved(self):
        a, b = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY))
        apply_dispute_locks([a, b])
        with pytest.raises(ValueError, match="dispute locked"):
            a.mark_auto_resolved()


class TestHealingRespectsDisputeLocks:
    def test_filter_healable_parcels_excludes_locked(self):
        a, b, c = (_orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY)), _orm(ID_C, "C", _poly_c(DX, DY)))
        apply_dispute_locks([a, b, c])
        assert filter_healable_parcels([c, b, a]) == (c,)

    def test_micro_overlap_healing_never_touches_locked_parcels(self):
        a, b, c = (_orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY)), _orm(ID_C, "C", _poly_c(DX, DY)))
        apply_dispute_locks([a, b, c])
        snapshot = snapshot_locked_geometries([a, b, c])
        c_before = _wkb(c)

        result = heal_layer_micro_overlaps([a, b, c])

        assert result.resolved_pairs == ()
        assert (ID_A, ID_C) in result.skipped_critical_pairs
        assert _wkb(c) == c_before
        assert c.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        assert_locked_geometries_preserved([a, b, c], snapshot)

    def test_sliver_healing_never_merges_into_locked_parcel(self):
        a = _orm(ID_A, "A", _rect(0.0, 0.0, 10.0, 40.0, DX, DY), locked=True)
        s = _orm(ID_D, "S", _rect(10.0, 0.0, 10.1, 40.0, DX, DY), sliver=True)
        snapshot = snapshot_locked_geometries([a, s])

        result = heal_layer_slivers([a, s])

        assert result.unassigned_sliver_ids == (ID_D,)
        assert_locked_geometries_preserved([a, s], snapshot)


class TestLockedGeometryGuard:
    def test_snapshot_contains_only_locked_parcels(self):
        a, b, c = (_orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY)), _orm(ID_C, "C", _poly_c(DX, DY)))
        apply_dispute_locks([a, b, c])

        snapshot = snapshot_locked_geometries([a, b, c])

        assert set(snapshot) == {ID_A, ID_B}
        assert snapshot[ID_A] == _wkb(a)

    def test_guard_raises_when_locked_projected_geometry_changes(self):
        a, b = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY))
        apply_dispute_locks([a, b])
        snapshot = snapshot_locked_geometries([a, b])

        a.geometry_projected = from_shape(MultiPolygon([_rect(0.0, 0.0, 95.0, 100.0, DX, DY)]), srid=32643)

        with pytest.raises(DisputedGeometryModifiedError, match=str(ID_A)):
            assert_locked_geometries_preserved([a, b], snapshot)

    def test_guard_raises_when_locked_wgs84_geometry_changes(self):
        a, b = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY))
        apply_dispute_locks([a, b])
        snapshot = snapshot_locked_geometries([a, b])

        shifted = MultiPolygon([_rect(1.0, 0.0, 101.0, 100.0, DX, DY)])
        b.geometry_wgs84 = from_shape(project_metric_geometry_to_wgs84(shifted), srid=4326)

        with pytest.raises(DisputedGeometryModifiedError, match=str(ID_B)):
            assert_locked_geometries_preserved([a, b], snapshot)

    def test_guard_raises_when_locked_parcel_is_missing(self):
        a, b = _orm(ID_A, "A", _poly_a(DX, DY)), _orm(ID_B, "B", _poly_b(DX, DY))
        apply_dispute_locks([a, b])
        snapshot = snapshot_locked_geometries([a, b])

        with pytest.raises(DisputedGeometryModifiedError, match=str(ID_B)):
            assert_locked_geometries_preserved([a], snapshot)

    def test_guard_error_is_a_value_error(self):
        assert issubclass(DisputedGeometryModifiedError, ValueError)
