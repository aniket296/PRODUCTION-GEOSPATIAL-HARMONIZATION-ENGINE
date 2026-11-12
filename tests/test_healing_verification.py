"""Tests for post-resolution verification and audit timestamps (Step 102/150).

Directive Sections 20-25:
- Following atomic healing execution, the post-resolution verifier re-checks healed active parcels in projected metric
  coordinates (`EPSG:32643`) to ensure:
  1. Every active parcel has a valid, non-empty polygonal geometry.
  2. Zero residual `AUTO_RESOLVABLE` overlaps, slivers, or sub-tolerance boundary gaps remain between active parcels.
  3. All `CRITICAL_DISPUTE` topological errors remain unresolved (`is_resolved=False`, `resolved_at=None`) and their
     locked parcel geometries remain intact.
  4. Resolved `AUTO_RESOLVABLE` `TopologicalError` records are stamped via `TopologicalError.resolve(...)` with
     `is_resolved=True`, a deterministic `resolution_method`, `resolved_by`, `resolution_details`, and timezone-aware
     UTC audit timestamps (`resolved_at >= created_at`, `updated_at >= resolved_at`).
  5. When `strict=True`, any lingering `AUTO_RESOLVABLE` defect or invalid geometry after healing raises
     `PostResolutionVerificationError` (a `ValueError`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.healing.orchestrator import execute_layer_healing_pipeline
from backend.healing.verification import (
    PostResolutionVerificationError,
    PostResolutionVerificationResult,
    verify_and_audit_healing,
    verify_layer_post_resolution,
)
from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.topology.recorder import TopologyConflictRecorder, project_metric_geometry_to_wgs84

LAYER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_C = uuid.UUID("00000000-0000-0000-0000-000000000003")
ID_D = uuid.UUID("00000000-0000-0000-0000-000000000004")
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
        dispute_locked=locked,
        is_disputed=locked,
        is_sliver=sliver,
    )


def _error_record(
    *,
    error_type: TopologyErrorType,
    severity: TopologySeverity,
    poly: Polygon,
    primary_id: uuid.UUID,
    secondary_id: uuid.UUID | None = None,
) -> TopologicalError:
    return TopologicalError(
        id=uuid.uuid4(),
        layer_id=LAYER_ID,
        primary_parcel_id=primary_id,
        secondary_parcel_id=secondary_id,
        error_type=error_type.value,
        severity=severity.value,
        error_geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(poly), srid=4326),
        error_geometry_projected=from_shape(poly, srid=32643),
        affected_area_sqm=float(poly.area),
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


class TestPostResolutionVerificationAndAudit:
    def test_stamps_resolved_auto_resolvable_errors_with_utc_audit_timestamps(self):
        # A [0, 100]^2 and B [99, 199]x[0, 100] have a 1% micro-overlap; S [-0.1, 0]x[0, 40] is a 4 m^2 sliver
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_b = _orm(ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, DX, DY))
        p_s = _orm(ID_S, "S", _rect(-0.1, 0.0, 0.0, 40.0, DX, DY), sliver=True)

        recorder = TopologyConflictRecorder()
        errors = recorder.build_layer_errors(LAYER_ID, [p_a, p_b, p_s])
        for err in errors:
            if err.id is None:
                err.id = uuid.uuid4()
            if err.created_at is None:
                err.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        pipeline_result = execute_layer_healing_pipeline([p_a, p_b, p_s])
        verification = verify_and_audit_healing(
            [p_a, p_b, p_s],
            errors,
            pipeline_result=pipeline_result,
            resolved_by="auto-healing-engine",
        )

        assert isinstance(verification, PostResolutionVerificationResult)
        assert verification.passed is True
        assert verification.residual_auto_resolvable_count == 0
        assert verification.residual_critical_dispute_count == 0
        assert verification.invalid_parcel_ids == ()
        assert verification.valid_parcel_count == 3
        assert verification.verified_at.tzinfo == timezone.utc

        resolved_errors = [e for e in errors if e.is_resolved]
        assert len(resolved_errors) == len(errors)
        assert set(verification.resolved_error_ids) == {e.id for e in resolved_errors}

        for err in resolved_errors:
            assert err.is_resolved is True
            assert err.resolved_by == "auto-healing-engine"
            assert err.resolution_method in {
                "SLIVER_UNION",
                "MICRO_OVERLAP_CENTERLINE_SPLIT",
                "BOUNDARY_SNAP",
                "GRID_SNAP",
                "AUTO_HEALING_PIPELINE",
            }
            assert isinstance(err.resolved_at, datetime)
            assert err.resolved_at.tzinfo == timezone.utc
            assert err.resolved_at >= err.created_at
            assert err.updated_at >= err.resolved_at
            assert err.resolution_details is not None
            assert "verified_at" in err.resolution_details

    def test_critical_dispute_errors_remain_unresolved_and_untouched(self):
        # C [0, 100]^2 and D [90, 190]x[0, 100] overlap 10% (CRITICAL_DISPUTE)
        # A [300, 400]^2 and B [399, 499]x[300, 400] overlap 1% (AUTO_RESOLVABLE)
        p_a = _orm(ID_A, "A", _rect(300.0, 300.0, 400.0, 400.0, DX, DY))
        p_b = _orm(ID_B, "B", _rect(399.0, 300.0, 499.0, 400.0, DX, DY))
        p_c = _orm(ID_C, "C", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_d = _orm(ID_D, "D", _rect(90.0, 0.0, 190.0, 100.0, DX, DY))

        err_auto = _error_record(
            error_type=TopologyErrorType.OVERLAP,
            severity=TopologySeverity.AUTO_RESOLVABLE,
            poly=_rect(399.0, 300.0, 400.0, 400.0, DX, DY),
            primary_id=ID_A,
            secondary_id=ID_B,
        )
        err_crit = _error_record(
            error_type=TopologyErrorType.OVERLAP,
            severity=TopologySeverity.CRITICAL_DISPUTE,
            poly=_rect(90.0, 0.0, 100.0, 100.0, DX, DY),
            primary_id=ID_C,
            secondary_id=ID_D,
        )

        pipeline_result = execute_layer_healing_pipeline([p_a, p_b, p_c, p_d])
        verification = verify_and_audit_healing(
            [p_a, p_b, p_c, p_d],
            [err_auto, err_crit],
            pipeline_result=pipeline_result,
        )

        assert verification.passed is True
        assert verification.resolved_error_ids == (err_auto.id,)
        assert verification.remaining_dispute_error_ids == (err_crit.id,)
        assert verification.residual_critical_dispute_count >= 1

        assert err_auto.is_resolved is True
        assert err_auto.resolved_at is not None

        assert err_crit.is_resolved is False
        assert err_crit.resolved_at is None
        assert err_crit.resolution_method is None
        assert err_crit.resolved_by is None

    def test_strict_verification_raises_if_auto_resolvable_overlap_still_remains(self):
        # Simulate parcels that were NOT healed (still have a 1% micro-overlap)
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_b = _orm(ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, DX, DY))
        err_auto = _error_record(
            error_type=TopologyErrorType.OVERLAP,
            severity=TopologySeverity.AUTO_RESOLVABLE,
            poly=_rect(99.0, 0.0, 100.0, 100.0, DX, DY),
            primary_id=ID_A,
            secondary_id=ID_B,
        )

        with pytest.raises(PostResolutionVerificationError, match="residual"):
            verify_and_audit_healing([p_a, p_b], [err_auto], strict=True)

        assert err_auto.is_resolved is False
        assert err_auto.resolved_at is None

    def test_non_strict_verification_reports_failure_without_raising(self):
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_b = _orm(ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, DX, DY))

        result = verify_and_audit_healing([p_a, p_b], [], strict=False)

        assert result.passed is False
        assert result.residual_auto_resolvable_count >= 1

    def test_async_verify_layer_post_resolution_flushes_on_pass(self):
        def _drive(coro):
            try:
                while True:
                    coro.send(None)
            except StopIteration as stop:
                return stop.value

        session = AsyncMock()
        p_a = _orm(ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, DX, DY))
        p_b = _orm(ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, DX, DY))
        err_auto = _error_record(
            error_type=TopologyErrorType.OVERLAP,
            severity=TopologySeverity.AUTO_RESOLVABLE,
            poly=_rect(99.0, 0.0, 100.0, 100.0, DX, DY),
            primary_id=ID_A,
            secondary_id=ID_B,
        )

        pipeline_result = execute_layer_healing_pipeline([p_a, p_b])
        res = _drive(
            verify_layer_post_resolution(
                session,
                [p_a, p_b],
                [err_auto],
                pipeline_result=pipeline_result,
            )
        )

        assert res.passed is True
        assert err_auto.is_resolved is True
        session.flush.assert_awaited_once()

    def test_verification_error_is_a_value_error(self):
        assert issubclass(PostResolutionVerificationError, ValueError)
