"""Tests for deterministic boundary snapping with displacement and validity assertions (Step 96/150).

Directive Sections 18, 20-23, 25:
- Sub-tolerance boundary gaps between adjacent parcels are closed by snapping parcel vertices onto reference parcel
  vertices/segments (`ST_Snap` / `shapely.ops.snap`) in projected metric coordinates (EPSG:32643).
- Default tolerance is `Settings.snap_healing_tolerance_meters` (0.25 m); tolerance must be > 0 and finite.
- Displacement assertion: the Hausdorff distance between the original and snapped geometry must never exceed the
  tolerance.
- Validity assertion: the snapped geometry must be a valid, non-empty MultiPolygon with positive area (snapping is
  never silently repaired); otherwise `SnapValidityError` (a `ValueError`) is raised and the batch leaves the parcel
  unchanged (`rejected_parcel_ids`).
- Batch determinism: parcels are processed in ascending UUID order; each unlocked parcel snaps to the current geometry
  of every lower-UUID parcel plus every dispute-locked parcel. Dispute-locked parcels are fixed anchors and are never
  modified.

Expected API in `backend.healing.boundary_snap`:
- `SnapCandidateInput(parcel_id, geometry, dispute_locked=False)`
- `BoundarySnapResult(parcel_id, geometry_after, max_displacement_m, area_before_sqm, area_after_sqm, snapped)`
- `BoundarySnapBatchResult(results, updated_geometries, skipped_locked_ids, rejected_parcel_ids)`
- `SnapValidityError(ValueError)`
- `measure_max_displacement_m(before, after) -> float`
- `snap_geometry_to_reference(geometry, reference, *, tolerance_m=None, parcel_id=None) -> BoundarySnapResult`
- `snap_parcel_boundaries(candidates, *, tolerance_m=None) -> BoundarySnapBatchResult`
- `snap_layer_boundaries(parcels, *, tolerance_m=None) -> BoundarySnapBatchResult`
- `boundary_snap_geometry_expr(geom, reference, tolerance_m) -> SQL expression`
"""

from __future__ import annotations

import uuid

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import column, table
from sqlalchemy.dialects import postgresql

from backend.healing.boundary_snap import (
    BoundarySnapBatchResult,
    BoundarySnapResult,
    SnapCandidateInput,
    SnapValidityError,
    boundary_snap_geometry_expr,
    measure_max_displacement_m,
    snap_geometry_to_reference,
    snap_layer_boundaries,
    snap_parcel_boundaries,
)
from backend.healing.shared_boundary import compute_shared_boundary_length_m
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84

PG = postgresql.dialect()

ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_C = uuid.UUID("00000000-0000-0000-0000-000000000003")

DX, DY = 500_000.0, 3_000_000.0


def _rect(x0: float, y0: float, x1: float, y1: float, dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return Polygon(
        [(x0 + dx, y0 + dy), (x1 + dx, y0 + dy), (x1 + dx, y1 + dy), (x0 + dx, y1 + dy), (x0 + dx, y0 + dy)]
    )


def _west(dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return _rect(0.0, 0.0, 100.0, 100.0, dx, dy)


def _east(gap: float = 0.2, dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return _rect(100.0 + gap, 0.0, 200.0, 100.0, dx, dy)


def _north(gap: float = 0.1) -> Polygon:
    return _rect(0.0, 100.0 + gap, 100.0, 200.0 + gap)


def _cand(pid: uuid.UUID, poly: Polygon, *, locked: bool = False) -> SnapCandidateInput:
    return SnapCandidateInput(parcel_id=pid, geometry=poly, dispute_locked=locked)


def _self_intersection_bait() -> Polygon:
    """Valid sliver beside West's NE corner; snapping (100.13, 99.93)->(100, 100) makes the ring self-intersect."""
    return Polygon([(100.01, 99.73), (100.24, 100.46), (100.48, 99.89), (100.13, 99.93), (100.01, 99.73)])


class TestMeasureDisplacement:
    def test_identical_geometries_have_zero_displacement(self):
        assert measure_max_displacement_m(_west(), _west()) == pytest.approx(0.0, abs=1e-12)

    def test_displacement_is_hausdorff_distance(self):
        assert measure_max_displacement_m(_east(0.2), _east(0.0)) == pytest.approx(0.2, abs=1e-9)


class TestSnapGeometryToReference:
    def test_sub_tolerance_gap_is_closed(self):
        result = snap_geometry_to_reference(_east(0.2), _west(), parcel_id=ID_B)

        assert isinstance(result, BoundarySnapResult)
        assert result.parcel_id == ID_B
        assert result.snapped is True
        assert isinstance(result.geometry_after, MultiPolygon)
        assert result.geometry_after.is_valid
        assert result.geometry_after.bounds == pytest.approx((100.0, 0.0, 200.0, 100.0), abs=1e-9)
        assert result.area_before_sqm == pytest.approx(9_980.0, abs=1e-6)
        assert result.area_after_sqm == pytest.approx(10_000.0, abs=1e-6)
        assert result.max_displacement_m == pytest.approx(0.2, abs=1e-9)
        assert compute_shared_boundary_length_m(result.geometry_after, _west()) == pytest.approx(100.0, abs=1e-9)
        assert result.geometry_after.union(_west()).area == pytest.approx(20_000.0, abs=1e-6)

    def test_displacement_never_exceeds_tolerance(self):
        # Staggered edge: vertices offset 0.1 m and 0.2 m from the reference edge.
        staggered = Polygon([(100.1, 0.0), (200.0, 0.0), (200.0, 100.0), (100.2, 100.0), (100.1, 0.0)])
        result = snap_geometry_to_reference(staggered, _west(), tolerance_m=0.25)

        assert result.snapped is True
        assert result.max_displacement_m <= 0.25 + 1e-12
        assert result.geometry_after.is_valid
        assert result.geometry_after.bounds[0] == pytest.approx(100.0, abs=1e-9)

    def test_gap_above_tolerance_is_left_unchanged(self):
        original = _east(0.4)
        result = snap_geometry_to_reference(original, _west())

        assert result.snapped is False
        assert result.max_displacement_m == pytest.approx(0.0, abs=1e-12)
        assert result.geometry_after.equals(MultiPolygon([original]))
        assert result.area_after_sqm == pytest.approx(result.area_before_sqm, abs=1e-9)

    def test_custom_tolerance_allows_wider_snap(self):
        result = snap_geometry_to_reference(_east(0.4), _west(), tolerance_m=0.5)
        assert result.snapped is True
        assert result.max_displacement_m == pytest.approx(0.4, abs=1e-9)

    def test_already_aligned_parcel_is_not_snapped(self):
        result = snap_geometry_to_reference(_east(0.0), _west())
        assert result.snapped is False
        assert result.max_displacement_m == pytest.approx(0.0, abs=1e-12)

    def test_snap_producing_invalid_geometry_raises_validity_error(self):
        # Thin valid quadrilateral whose snapped vertices cross each other -> self-intersecting (invalid) ring.
        bait = _self_intersection_bait()
        assert bait.is_valid
        with pytest.raises(SnapValidityError, match="invalid"):
            snap_geometry_to_reference(bait, _west(), parcel_id=ID_C)

    def test_validity_error_is_a_value_error(self):
        assert issubclass(SnapValidityError, ValueError)

    @pytest.mark.parametrize("bad", [0.0, -0.25, float("nan"), float("inf")])
    def test_invalid_tolerance_raises_value_error(self, bad):
        with pytest.raises(ValueError, match="tolerance_m"):
            snap_geometry_to_reference(_east(0.2), _west(), tolerance_m=bad)


class TestSnapParcelBoundariesBatch:
    def test_higher_uuid_parcels_snap_onto_lower_uuid_anchor(self):
        result = snap_parcel_boundaries([_cand(ID_B, _east(0.2)), _cand(ID_A, _west())])

        assert isinstance(result, BoundarySnapBatchResult)
        assert [r.parcel_id for r in result.results] == [ID_A, ID_B]
        assert result.results[0].snapped is False
        assert result.results[1].snapped is True
        assert set(result.updated_geometries) == {ID_B}
        assert result.updated_geometries[ID_B].bounds == pytest.approx((100.0, 0.0, 200.0, 100.0), abs=1e-9)
        assert result.skipped_locked_ids == ()
        assert result.rejected_parcel_ids == ()

    def test_batch_is_independent_of_input_order(self):
        cands = [_cand(ID_A, _west()), _cand(ID_B, _east(0.2)), _cand(ID_C, _north(0.1))]
        baseline = snap_parcel_boundaries(cands)

        for perm in (cands[::-1], [cands[2], cands[0], cands[1]]):
            again = snap_parcel_boundaries(perm)
            assert set(again.updated_geometries) == set(baseline.updated_geometries) == {ID_B, ID_C}
            for pid, geom in baseline.updated_geometries.items():
                assert again.updated_geometries[pid].wkb == geom.wkb

    def test_all_snapped_results_respect_tolerance_and_validity(self):
        result = snap_parcel_boundaries(
            [_cand(ID_A, _west()), _cand(ID_B, _east(0.2)), _cand(ID_C, _north(0.1))]
        )
        for r in result.results:
            assert r.max_displacement_m <= 0.25 + 1e-12
            assert r.geometry_after.is_valid
            assert r.area_after_sqm > 0.0
        assert result.updated_geometries[ID_C].bounds[1] == pytest.approx(100.0, abs=1e-9)

    def test_dispute_locked_parcel_is_fixed_anchor_and_never_modified(self):
        # East (higher UUID) is locked: it is skipped, and the unlocked West snaps onto it instead.
        result = snap_parcel_boundaries([_cand(ID_A, _west()), _cand(ID_B, _east(0.2), locked=True)])

        assert result.skipped_locked_ids == (ID_B,)
        assert ID_B not in result.updated_geometries
        assert set(result.updated_geometries) == {ID_A}
        assert result.updated_geometries[ID_A].bounds == pytest.approx((0.0, 0.0, 100.2, 100.0), abs=1e-9)
        assert all(r.parcel_id != ID_B for r in result.results)

    def test_invalid_snap_result_is_rejected_and_left_unchanged(self):
        result = snap_parcel_boundaries([_cand(ID_A, _west()), _cand(ID_C, _self_intersection_bait())])

        assert result.rejected_parcel_ids == (ID_C,)
        assert ID_C not in result.updated_geometries


def _orm(pid: uuid.UUID, ident: str, poly: Polygon, *, locked: bool = False) -> HarmonizedParcel:
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
    )


class TestSnapLayerBoundariesOnParcels:
    def test_snapped_parcel_updates_both_crs_and_metrics(self):
        a = _orm(ID_A, "A", _west(DX, DY))
        b = _orm(ID_B, "B", _east(0.2, DX, DY))
        a_wkb = bytes(a.geometry_projected.data)

        result = snap_layer_boundaries([b, a])

        assert set(result.updated_geometries) == {ID_B}
        assert b.geometry_projected.srid == 32643
        assert b.geometry_wgs84.srid == 4326
        assert to_shape(b.geometry_projected).geom_type == "MultiPolygon"
        assert to_shape(b.geometry_wgs84).geom_type == "MultiPolygon"
        assert b.metric_area_sqm == pytest.approx(10_000.0, abs=1e-3)
        assert b.metric_perimeter_m == pytest.approx(400.0, abs=1e-3)
        assert b.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value

        assert bytes(a.geometry_projected.data) == a_wkb
        assert a.resolution_status == ParcelResolutionStatus.ORIGINAL.value

    def test_locked_orm_parcel_geometry_is_preserved(self):
        a = _orm(ID_A, "A", _west(DX, DY))
        b = _orm(ID_B, "B", _east(0.2, DX, DY), locked=True)
        b_before = (bytes(b.geometry_projected.data), bytes(b.geometry_wgs84.data))

        snap_layer_boundaries([a, b])

        assert (bytes(b.geometry_projected.data), bytes(b.geometry_wgs84.data)) == b_before
        assert a.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value


class TestBoundarySnapSQL:
    def test_expression_uses_st_snap_with_metric_tolerance(self):
        p = table("p", column("geometry_projected"))
        r = table("r", column("geometry_projected"))
        sql = str(
            boundary_snap_geometry_expr(p.c.geometry_projected, r.c.geometry_projected, 0.25).compile(
                dialect=PG, compile_kwargs={"literal_binds": True}
            )
        )

        assert "ST_Snap(p.geometry_projected, r.geometry_projected, 0.25)" in sql
        assert "ST_MakeValid(" in sql
        assert "ST_CollectionExtract(" in sql
        assert "ST_Multi(" in sql

    def test_expression_rejects_invalid_tolerance(self):
        p = table("p", column("geometry_projected"))
        with pytest.raises(ValueError, match="tolerance_m"):
            boundary_snap_geometry_expr(p.c.geometry_projected, p.c.geometry_projected, -1.0)
