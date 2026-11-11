"""Tests for deterministic metric grid normalization (Step 98/150).

Directive Sections 19, 20-23, 25:
- Floating-point drift on healed projected geometries (`EPSG:32643`) is eliminated by snapping coordinates to a fixed
  metric grid (`Settings.grid_tolerance_meters`, default `0.001` m = 1 millimeter) via `ST_SnapToGrid` / deterministic
  coordinate rounding.
- Sub-millimeter jitter between adjacent parcels is removed so shared boundaries become topologically exact.
- Dispute-locked parcels (`dispute_locked=True`) are never modified (`skipped_locked_ids`).
- Sub-grid degenerate polygons that collapse or become invalid when snapped to the grid raise `GridSnapValidityError`
  (a `ValueError`) and are recorded in `rejected_parcel_ids` during batch execution.
"""

from __future__ import annotations

import math
import uuid

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import column, table
from sqlalchemy.dialects import postgresql

from backend.healing.grid_snap import (
    GridSnapBatchResult,
    GridSnapCandidateInput,
    GridSnapResult,
    GridSnapValidityError,
    grid_snap_geometry_expr,
    normalize_layer_grid,
    normalize_parcel_grid,
    snap_geometry_to_grid,
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


def _cand(pid: uuid.UUID, poly: Polygon, *, locked: bool = False) -> GridSnapCandidateInput:
    return GridSnapCandidateInput(parcel_id=pid, geometry=poly, dispute_locked=locked)


class TestSnapGeometryToGrid:
    def test_eliminates_sub_millimeter_floating_point_drift(self):
        # Coordinates have sub-millimeter drift around [0, 100] x [0, 100]
        drifted = Polygon(
            [
                (0.00019, -0.00024),
                (100.00031, 0.00011),
                (99.99972, 100.00042),
                (-0.00018, 99.99981),
                (0.00019, -0.00024),
            ]
        )

        result = snap_geometry_to_grid(drifted, parcel_id=ID_A)

        assert isinstance(result, GridSnapResult)
        assert result.parcel_id == ID_A
        assert result.normalized is True
        assert isinstance(result.geometry_after, MultiPolygon)
        assert result.geometry_after.is_valid
        assert result.geometry_after.bounds == pytest.approx((0.0, 0.0, 100.0, 100.0), abs=1e-12)
        assert result.area_after_sqm == pytest.approx(10_000.0, abs=1e-9)
        assert result.max_displacement_m <= math.hypot(0.0005, 0.0005) + 1e-9

    def test_adjacent_parcels_with_sub_millimeter_drift_share_exact_boundary_after_grid_snap(self):
        west = _rect(0.0, 0.0, 100.00022, 100.0)
        east = _rect(99.99984, 0.0, 200.0, 100.0)

        res_w = snap_geometry_to_grid(west, parcel_id=ID_A)
        res_e = snap_geometry_to_grid(east, parcel_id=ID_B)

        assert compute_shared_boundary_length_m(res_w.geometry_after, res_e.geometry_after) == pytest.approx(
            100.0, abs=1e-9
        )
        assert res_w.geometry_after.intersection(res_e.geometry_after).area == pytest.approx(0.0, abs=1e-12)

    def test_already_on_grid_geometry_is_flagged_unmodified(self):
        clean = _rect(0.0, 0.0, 100.0, 100.0)
        result = snap_geometry_to_grid(clean, parcel_id=ID_A)

        assert result.normalized is False
        assert result.max_displacement_m == pytest.approx(0.0, abs=1e-12)
        assert result.geometry_after.equals(MultiPolygon([clean]))

    def test_sub_grid_polygon_collapsing_to_zero_area_raises_validity_error(self):
        # 0.0002 m x 0.0002 m square rounds all 4 vertices to (10.0, 10.0) on a 1 mm grid -> collapses
        micro = _rect(10.0001, 10.0001, 10.0003, 10.0003)
        with pytest.raises(GridSnapValidityError, match="collapse|invalid|empty"):
            snap_geometry_to_grid(micro, parcel_id=ID_C)

    def test_validity_error_is_subclass_of_value_error(self):
        assert issubclass(GridSnapValidityError, ValueError)

    @pytest.mark.parametrize("bad_grid", [0.0, -0.001, float("nan"), float("inf")])
    def test_invalid_grid_size_raises_value_error(self, bad_grid: float):
        with pytest.raises(ValueError, match="grid_size_m"):
            snap_geometry_to_grid(_rect(0.0, 0.0, 10.0, 10.0), grid_size_m=bad_grid)


class TestNormalizeParcelGridBatch:
    def test_normalizes_unlocked_parcels_and_skips_dispute_locked(self):
        drifted_a = _rect(0.0002, 0.0, 100.0002, 100.0)
        drifted_b = _rect(100.0003, 0.0, 200.0, 100.0)

        result = normalize_parcel_grid([_cand(ID_B, drifted_b, locked=True), _cand(ID_A, drifted_a)])

        assert isinstance(result, GridSnapBatchResult)
        assert [r.parcel_id for r in result.results] == [ID_A]
        assert set(result.updated_geometries) == {ID_A}
        assert result.updated_geometries[ID_A].bounds == pytest.approx((0.0, 0.0, 100.0, 100.0), abs=1e-12)
        assert result.skipped_locked_ids == (ID_B,)
        assert result.rejected_parcel_ids == ()

    def test_collapsing_parcel_is_recorded_in_rejected_ids(self):
        clean = _rect(0.0, 0.0, 100.0, 100.0)
        micro = _rect(10.0001, 10.0001, 10.0003, 10.0003)

        result = normalize_parcel_grid([_cand(ID_A, clean), _cand(ID_C, micro)])

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


class TestNormalizeLayerGridOnORM:
    def test_updates_drifted_orm_parcel_and_leaves_locked_parcel_untouched(self):
        a = _orm(ID_A, "A", _rect(0.00025, -0.00018, 100.00021, 100.00019, DX, DY))
        b = _orm(ID_B, "B", _rect(100.00031, 0.0, 200.0, 100.0, DX, DY), locked=True)
        b_before = (bytes(b.geometry_projected.data), bytes(b.geometry_wgs84.data))

        result = normalize_layer_grid([b, a])

        assert set(result.updated_geometries) == {ID_A}
        assert a.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert a.metric_area_sqm == pytest.approx(10_000.0, abs=1e-4)
        assert to_shape(a.geometry_projected).bounds == pytest.approx(
            (DX, DY, DX + 100.0, DY + 100.0), abs=1e-9
        )
        assert (bytes(b.geometry_projected.data), bytes(b.geometry_wgs84.data)) == b_before
        assert b.resolution_status == ParcelResolutionStatus.ORIGINAL.value


class TestGridSnapSQL:
    def test_grid_snap_geometry_expr_uses_st_snaptogrid(self):
        p = table("p", column("geometry_projected"))
        sql = str(
            grid_snap_geometry_expr(p.c.geometry_projected, 0.001).compile(
                dialect=PG, compile_kwargs={"literal_binds": True}
            )
        )

        assert "ST_SnapToGrid(p.geometry_projected, 0.001)" in sql
        assert "ST_MakeValid(" in sql
        assert "ST_CollectionExtract(" in sql
        assert "ST_Multi(" in sql

    def test_grid_snap_geometry_expr_rejects_non_positive_grid(self):
        p = table("p", column("geometry_projected"))
        with pytest.raises(ValueError, match="grid_size_m"):
            grid_snap_geometry_expr(p.c.geometry_projected, 0.0)
