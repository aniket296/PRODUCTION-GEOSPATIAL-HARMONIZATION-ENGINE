"""Tests for deterministic sliver assignment and union (Step 90/150).

Directive Sections 16, 20-22, 25: a metric sliver (area < 10 m² AND Polsby-Popper < 0.05) is merged into the adjacent
parcel that shares the LONGEST boundary with it. The healing must be deterministic, must never touch dispute-locked
parcels, must conserve area, must recalculate the receiving parcel's metrics, and must not destroy provenance.

Pure geometry (`heal_slivers`):
- Slivers are processed in ascending parcel UUID order; neighbors are evaluated against the CURRENT geometries, so
  several slivers can accumulate into one neighbor.
- Ties on shared boundary length resolve to the smallest neighbor UUID.
- Eligible neighbors are non-sliver and not dispute-locked. A sliver with no eligible neighbor, or a dispute-locked
  sliver, is reported in `unassigned_sliver_ids` and left unchanged.
- Result is independent of input ordering.

ORM application (`heal_layer_slivers`):
- The receiving parcel gets the merged MultiPolygon in both CRSs (SRID 32643 and 4326), recalculated area, perimeter
  and Polsby-Popper compactness, and `AUTO_RESOLVED` status.
- The sliver record is kept (original geometry preserved for audit), marked `AUTO_RESOLVED`, and annotated with
  `merged_into_parcel_id`.

PostGIS (`sliver_union_geometry_expr`): `ST_Multi(ST_CollectionExtract(ST_Union(neighbor, sliver), 3))`.
"""

from __future__ import annotations

import math
import uuid

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import column, table
from sqlalchemy.dialects import postgresql

from backend.healing.shared_boundary import SharedBoundaryCandidateInput
from backend.healing.sliver_union import (
    NeighborMetrics,
    SliverAssignment,
    SliverUnionResult,
    heal_layer_slivers,
    heal_slivers,
    sliver_union_geometry_expr,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.recorder import project_metric_geometry_to_wgs84

PG = postgresql.dialect()

ID_W = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_E = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_S = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _rect(x0: float, y0: float, x1: float, y1: float, dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return Polygon([(x0 + dx, y0 + dy), (x1 + dx, y0 + dy), (x1 + dx, y1 + dy), (x0 + dx, y1 + dy), (x0 + dx, y0 + dy)])


def _inp(pid, geom, *, sliver=False, locked=False) -> SharedBoundaryCandidateInput:
    return SharedBoundaryCandidateInput(parcel_id=pid, geometry=geom, is_sliver=sliver, dispute_locked=locked)


def _layout():
    """West 10x40 (400 m²), sliver 0.1x40 (4 m²), east 10x25 (250 m²) sharing only 25 m with the sliver."""
    west = _rect(0.0, 0.0, 10.0, 40.0)
    sliver = _rect(10.0, 0.0, 10.1, 40.0)
    east = _rect(10.1, 0.0, 20.1, 25.0)
    return west, sliver, east


class TestSliverAssignmentAndUnion:
    def test_sliver_is_merged_into_neighbor_with_longest_shared_boundary(self):
        west, sliver, east = _layout()
        result = heal_slivers([_inp(ID_W, west), _inp(ID_E, east), _inp(ID_S, sliver, sliver=True)])

        assert isinstance(result, SliverUnionResult)
        assert len(result.assignments) == 1
        a = result.assignments[0]
        assert isinstance(a, SliverAssignment)
        assert a.sliver_parcel_id == ID_S
        assert a.neighbor_parcel_id == ID_W
        assert a.shared_length_m == pytest.approx(40.0, abs=1e-9)
        assert a.sliver_area_sqm == pytest.approx(4.0, abs=1e-9)
        assert a.neighbor_area_before_sqm == pytest.approx(400.0, abs=1e-9)
        assert a.neighbor_area_after_sqm == pytest.approx(404.0, abs=1e-9)
        assert result.unassigned_sliver_ids == ()

        merged = result.updated_geometries[ID_W]
        assert isinstance(merged, MultiPolygon)
        assert merged.is_valid
        assert len(merged.geoms) == 1
        assert merged.area == pytest.approx(404.0, abs=1e-9)
        assert merged.bounds == pytest.approx((0.0, 0.0, 10.1, 40.0))
        assert set(result.updated_geometries) == {ID_W}

    def test_neighbor_metrics_are_recalculated(self):
        west, sliver, east = _layout()
        result = heal_slivers([_inp(ID_W, west), _inp(ID_E, east), _inp(ID_S, sliver, sliver=True)])

        m = result.neighbor_metrics[ID_W]
        assert isinstance(m, NeighborMetrics)
        assert m.parcel_id == ID_W
        assert m.area_sqm == pytest.approx(404.0, abs=1e-9)
        assert m.perimeter_m == pytest.approx(100.2, abs=1e-9)
        assert m.polsby_popper == pytest.approx(4.0 * math.pi * 404.0 / (100.2**2), abs=1e-9)
        assert ID_E not in result.neighbor_metrics

    def test_ties_resolve_to_smallest_neighbor_uuid_regardless_of_input_order(self):
        west = _rect(0.0, 0.0, 10.0, 40.0)
        sliver = _rect(10.0, 0.0, 10.1, 40.0)
        east = _rect(10.1, 0.0, 20.1, 40.0)  # also shares the full 40 m

        forward = heal_slivers([_inp(ID_W, west), _inp(ID_E, east), _inp(ID_S, sliver, sliver=True)])
        reverse = heal_slivers([_inp(ID_S, sliver, sliver=True), _inp(ID_E, east), _inp(ID_W, west)])

        assert forward.assignments[0].neighbor_parcel_id == ID_W
        assert reverse.assignments[0].neighbor_parcel_id == ID_W
        assert forward.assignments == reverse.assignments
        assert forward.updated_geometries[ID_W].wkb == reverse.updated_geometries[ID_W].wkb

    def test_dispute_locked_neighbor_never_receives_sliver(self):
        west, sliver, east = _layout()
        result = heal_slivers(
            [_inp(ID_W, west, locked=True), _inp(ID_E, east), _inp(ID_S, sliver, sliver=True)]
        )

        assert result.assignments[0].neighbor_parcel_id == ID_E
        assert result.assignments[0].shared_length_m == pytest.approx(25.0, abs=1e-9)
        merged = result.updated_geometries[ID_E]
        assert merged.is_valid
        assert merged.area == pytest.approx(254.0, abs=1e-9)
        assert ID_W not in result.updated_geometries

    def test_sliver_with_only_locked_neighbor_is_unassigned_and_unchanged(self):
        west, sliver, _ = _layout()
        result = heal_slivers([_inp(ID_W, west, locked=True), _inp(ID_S, sliver, sliver=True)])

        assert result.assignments == ()
        assert result.unassigned_sliver_ids == (ID_S,)
        assert result.updated_geometries == {}
        assert result.neighbor_metrics == {}

    def test_dispute_locked_sliver_is_not_healed(self):
        west, sliver, _ = _layout()
        result = heal_slivers([_inp(ID_W, west), _inp(ID_S, sliver, sliver=True, locked=True)])

        assert result.assignments == ()
        assert result.unassigned_sliver_ids == (ID_S,)
        assert result.updated_geometries == {}

    def test_isolated_sliver_is_unassigned(self):
        _, sliver, _ = _layout()
        far = _rect(500.0, 500.0, 520.0, 520.0)
        result = heal_slivers([_inp(ID_E, far), _inp(ID_S, sliver, sliver=True)])

        assert result.assignments == ()
        assert result.unassigned_sliver_ids == (ID_S,)

    def test_sliver_adjacent_only_to_another_sliver_is_unassigned(self):
        id_s1 = uuid.UUID("00000000-0000-0000-0000-000000000090")
        id_s2 = uuid.UUID("00000000-0000-0000-0000-000000000091")
        result = heal_slivers(
            [
                _inp(id_s1, _rect(10.0, 0.0, 10.1, 40.0), sliver=True),
                _inp(id_s2, _rect(10.1, 0.0, 10.2, 40.0), sliver=True),
            ]
        )
        assert result.assignments == ()
        assert result.unassigned_sliver_ids == (id_s1, id_s2)

    def test_multiple_slivers_accumulate_into_same_neighbor_in_uuid_order(self):
        id_s1 = uuid.UUID("00000000-0000-0000-0000-000000000090")
        id_s2 = uuid.UUID("00000000-0000-0000-0000-000000000091")
        west = _rect(0.0, 0.0, 10.0, 40.0)
        east_sliver = _rect(10.0, 0.0, 10.1, 40.0)
        west_sliver = _rect(-0.1, 0.0, 0.0, 40.0)

        result = heal_slivers(
            [
                _inp(id_s2, west_sliver, sliver=True),
                _inp(ID_W, west),
                _inp(id_s1, east_sliver, sliver=True),
            ]
        )

        assert [a.sliver_parcel_id for a in result.assignments] == [id_s1, id_s2]
        assert all(a.neighbor_parcel_id == ID_W for a in result.assignments)
        assert result.assignments[0].neighbor_area_after_sqm == pytest.approx(404.0, abs=1e-9)
        assert result.assignments[1].neighbor_area_before_sqm == pytest.approx(404.0, abs=1e-9)
        assert result.assignments[1].neighbor_area_after_sqm == pytest.approx(408.0, abs=1e-9)
        merged = result.updated_geometries[ID_W]
        assert merged.is_valid and len(merged.geoms) == 1
        assert merged.bounds == pytest.approx((-0.1, 0.0, 10.1, 40.0))
        assert result.neighbor_metrics[ID_W].area_sqm == pytest.approx(408.0, abs=1e-9)

    def test_total_area_is_conserved(self):
        west, sliver, east = _layout()
        before = west.area + sliver.area + east.area
        result = heal_slivers([_inp(ID_W, west), _inp(ID_E, east), _inp(ID_S, sliver, sliver=True)])

        after = result.updated_geometries[ID_W].area + east.area
        assert after == pytest.approx(before, abs=1e-9)

    def test_no_slivers_returns_empty_result(self):
        west, _, east = _layout()
        result = heal_slivers([_inp(ID_W, west), _inp(ID_E, east)])
        assert result.assignments == ()
        assert result.unassigned_sliver_ids == ()
        assert result.updated_geometries == {}
        assert result.neighbor_metrics == {}

    def test_heal_slivers_is_deterministic_across_runs_and_orderings(self):
        west, sliver, east = _layout()
        inputs = [_inp(ID_W, west), _inp(ID_E, east), _inp(ID_S, sliver, sliver=True)]
        baseline = heal_slivers(inputs)
        for perm in (inputs[::-1], [inputs[1], inputs[2], inputs[0]]):
            again = heal_slivers(perm)
            assert again.assignments == baseline.assignments
            assert again.unassigned_sliver_ids == baseline.unassigned_sliver_ids
            assert again.updated_geometries[ID_W].wkb == baseline.updated_geometries[ID_W].wkb
            assert again.neighbor_metrics == baseline.neighbor_metrics

    def test_negative_area_tolerance_raises_value_error(self):
        west, sliver, _ = _layout()
        with pytest.raises(ValueError, match="area_tolerance_sqm"):
            heal_slivers([_inp(ID_W, west), _inp(ID_S, sliver, sliver=True)], area_tolerance_sqm=-1.0)


def _orm_parcel(layer_id, pid, ident, poly, *, locked=False, sliver=False) -> HarmonizedParcel:
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=pid,
        layer_id=layer_id,
        parcel_identifier=ident,
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        dispute_locked=locked,
        is_disputed=locked,
        is_sliver=sliver,
    )


class TestSliverUnionOnParcelRecords:
    """`heal_layer_slivers` applies the result to ORM parcels while preserving provenance."""

    def _parcels(self, layer_id, *, west_locked=False):
        dx, dy = 500_000.0, 3_000_000.0
        west, sliver, east = _layout()
        shift = lambda p: Polygon([(x + dx, y + dy) for x, y in p.exterior.coords])  # noqa: E731
        return (
            _orm_parcel(layer_id, ID_W, "W", shift(west), locked=west_locked),
            _orm_parcel(layer_id, ID_E, "E", shift(east)),
            _orm_parcel(layer_id, ID_S, "S", shift(sliver), sliver=True),
        )

    def test_neighbor_receives_merged_geometry_and_recalculated_metrics(self):
        layer_id = uuid.uuid4()
        w, e, s = self._parcels(layer_id)

        result = heal_layer_slivers([w, e, s])

        assert result.assignments[0].neighbor_parcel_id == ID_W
        merged_proj = to_shape(w.geometry_projected)
        assert merged_proj.geom_type == "MultiPolygon"
        assert merged_proj.area == pytest.approx(404.0, abs=1e-6)
        assert w.geometry_projected.srid == 32643
        assert w.geometry_wgs84.srid == 4326
        assert to_shape(w.geometry_wgs84).geom_type == "MultiPolygon"
        assert w.metric_area_sqm == pytest.approx(404.0, abs=1e-3)
        assert w.metric_perimeter_m == pytest.approx(100.2, abs=1e-3)
        assert w.polsby_popper_compactness == pytest.approx(4.0 * math.pi * 404.0 / (100.2**2), abs=1e-5)
        assert w.is_sliver is False
        assert w.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value

    def test_sliver_record_is_preserved_and_annotated(self):
        layer_id = uuid.uuid4()
        w, e, s = self._parcels(layer_id)
        original_wkb = bytes(s.geometry_projected.data)

        heal_layer_slivers([w, e, s])

        assert bytes(s.geometry_projected.data) == original_wkb
        assert to_shape(s.geometry_projected).area == pytest.approx(4.0, abs=1e-6)
        assert s.is_sliver is True
        assert s.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert s.attributes["merged_into_parcel_id"] == str(ID_W)

    def test_uninvolved_and_locked_parcels_are_untouched(self):
        layer_id = uuid.uuid4()
        w, e, s = self._parcels(layer_id, west_locked=True)
        e_wkb = bytes(e.geometry_projected.data)
        w_wkb = bytes(w.geometry_projected.data)

        heal_layer_slivers([w, e, s])

        assert bytes(w.geometry_projected.data) == w_wkb
        assert w.dispute_locked is True
        assert w.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        assert s.attributes is None or s.attributes.get("merged_into_parcel_id") == str(ID_E)
        assert bytes(e.geometry_projected.data) != e_wkb  # E received the sliver instead

    def test_unassigned_sliver_record_is_left_unchanged(self):
        layer_id = uuid.uuid4()
        far = _orm_parcel(layer_id, ID_E, "E", _rect(0.0, 0.0, 20.0, 20.0, 600_000.0, 3_100_000.0))
        sliver = _orm_parcel(
            layer_id, ID_S, "S", _rect(10.0, 0.0, 10.1, 40.0, 500_000.0, 3_000_000.0), sliver=True
        )

        result = heal_layer_slivers([far, sliver])

        assert result.unassigned_sliver_ids == (ID_S,)
        assert sliver.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        assert sliver.attributes is None


class TestSliverUnionSQL:
    def test_sliver_union_geometry_expr_unions_and_extracts_polygons(self):
        n = table("n", column("geometry_projected"))
        s = table("s", column("geometry_projected"))
        sql = str(sliver_union_geometry_expr(n.c.geometry_projected, s.c.geometry_projected).compile(dialect=PG))

        assert "ST_Multi(" in sql
        assert "ST_CollectionExtract(" in sql
        assert "ST_Union(n.geometry_projected, s.geometry_projected)" in sql
        assert ", 3)" in sql
