"""Tests for deterministic micro-overlap centerline splitting (Step 92/150).

Directive Sections 15, 20-23, 25:
- Pairwise overlaps where BOTH independent parcel overlap percentages are within `max_overlap_percent_threshold`
  (default <= 2.0%) and neither parcel is `dispute_locked` are classified as `AUTO_RESOLVABLE` micro-overlaps.
- Each micro-overlap is resolved deterministically in projected metric coordinates (`EPSG:32643`) by splitting the
  intersection polygon (`A ∩ B`) along its centerline into two equal-area halves assigned to `A_only` and `B_only`.
- After resolution:
  1. The polygonal overlap between the healed parcels is zero (`area(A' ∩ B') == 0`).
  2. No boundary gap is introduced (`area(A') + area(B') == area(A ∪ B)`).
  3. Each parcel receives half of the overlap area (`area(A') == area(A) - 0.5 * area(A ∩ B)`).
  4. Results are independent of input ordering (`(A, B)` vs `(B, A)`).
  5. Overlaps exceeding `threshold_percent` on either parcel (even if one parcel is huge and the other is small) or
     involving a `dispute_locked` parcel are never modified and are reported in `skipped_critical_pairs`.
"""

from __future__ import annotations

import uuid

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiLineString, MultiPolygon, Polygon

from backend.healing.micro_overlap import (
    MicroOverlapBatchResult,
    MicroOverlapCandidateInput,
    MicroOverlapSplitResult,
    heal_layer_micro_overlaps,
    heal_micro_overlaps,
    resolve_pair_micro_overlap,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.topology.intersection import extract_polygonal_intersection
from backend.topology.recorder import project_metric_geometry_to_wgs84

ID_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
ID_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
ID_C = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _rect(x0: float, y0: float, x1: float, y1: float, dx: float = 0.0, dy: float = 0.0) -> Polygon:
    return Polygon(
        [
            (x0 + dx, y0 + dy),
            (x1 + dx, y0 + dy),
            (x1 + dx, y1 + dy),
            (x0 + dx, y1 + dy),
            (x0 + dx, y0 + dy),
        ]
    )


def _cand(pid: uuid.UUID, geom: Polygon | MultiPolygon, *, locked: bool = False) -> MicroOverlapCandidateInput:
    return MicroOverlapCandidateInput(parcel_id=pid, geometry=geom, dispute_locked=locked)


class TestPairMicroOverlapCenterlineSplit:
    """Direct pairwise micro-overlap centerline splitting (`resolve_pair_micro_overlap`)."""

    def test_axis_aligned_corridor_splits_down_exact_centerline(self):
        # West parcel: [0, 100] x [0, 100] = 10,000 m²
        # East parcel: [99, 199] x [0, 100] = 10,000 m²
        # Overlap strip: [99, 100] x [0, 100] = 100 m² (1.0% of each parcel <= 2.0% threshold)
        west = _rect(0.0, 0.0, 100.0, 100.0)
        east = _rect(99.0, 0.0, 199.0, 100.0)

        split = resolve_pair_micro_overlap(ID_A, west, ID_B, east)

        assert isinstance(split, MicroOverlapSplitResult)
        assert split.parcel_a_id == ID_A
        assert split.parcel_b_id == ID_B
        assert split.intersection_area_sqm == pytest.approx(100.0, abs=1e-6)
        assert split.overlap_percentage_a == pytest.approx(1.0, abs=1e-6)
        assert split.overlap_percentage_b == pytest.approx(1.0, abs=1e-6)

        # Centerline is at x = 99.5 from y = 0 to y = 100
        assert isinstance(split.centerline_geometry, MultiLineString)
        assert split.centerline_geometry.length == pytest.approx(100.0, abs=1e-6)
        assert split.centerline_geometry.bounds == pytest.approx((99.5, 0.0, 99.5, 100.0), abs=1e-6)

        assert isinstance(split.geometry_a_after, MultiPolygon)
        assert isinstance(split.geometry_b_after, MultiPolygon)
        assert split.geometry_a_after.is_valid
        assert split.geometry_b_after.is_valid
        assert split.geometry_a_after.bounds == pytest.approx((0.0, 0.0, 99.5, 100.0), abs=1e-6)
        assert split.geometry_b_after.bounds == pytest.approx((99.5, 0.0, 199.0, 100.0), abs=1e-6)
        assert split.area_a_before_sqm == pytest.approx(10_000.0, abs=1e-6)
        assert split.area_b_before_sqm == pytest.approx(10_000.0, abs=1e-6)
        assert split.area_a_after_sqm == pytest.approx(9_950.0, abs=1e-6)
        assert split.area_b_after_sqm == pytest.approx(9_950.0, abs=1e-6)

        # Zero residual overlap and full union footprint conservation
        residual = extract_polygonal_intersection(split.geometry_a_after, split.geometry_b_after)
        assert residual.area == pytest.approx(0.0, abs=1e-9)
        assert split.area_a_after_sqm + split.area_b_after_sqm == pytest.approx(
            west.union(east).area, abs=1e-6
        )

    def test_vertical_north_south_micro_overlap_splits_horizontally(self):
        # South: [0, 100] x [0, 100] = 10,000 m²
        # North: [0, 100] x [98.4, 198.4] = 10,000 m²
        # Overlap: [0, 100] x [98.4, 100.0] = 160 m² (1.6% of each parcel)
        south = _rect(0.0, 0.0, 100.0, 100.0)
        north = _rect(0.0, 98.4, 100.0, 198.4)

        split = resolve_pair_micro_overlap(ID_A, south, ID_B, north)

        assert split.intersection_area_sqm == pytest.approx(160.0, abs=1e-6)
        assert split.geometry_a_after.bounds == pytest.approx((0.0, 0.0, 100.0, 99.2), abs=1e-6)
        assert split.geometry_b_after.bounds == pytest.approx((0.0, 99.2, 100.0, 198.4), abs=1e-6)
        assert split.area_a_after_sqm == pytest.approx(9_920.0, abs=1e-6)
        assert split.area_b_after_sqm == pytest.approx(9_920.0, abs=1e-6)

    def test_argument_order_invariance(self):
        west = _rect(0.0, 0.0, 100.0, 100.0)
        east = _rect(99.0, 0.0, 199.0, 100.0)

        ab = resolve_pair_micro_overlap(ID_A, west, ID_B, east)
        ba = resolve_pair_micro_overlap(ID_B, east, ID_A, west)

        assert ab.parcel_a_id == ba.parcel_a_id == ID_A
        assert ab.parcel_b_id == ba.parcel_b_id == ID_B
        assert ab.geometry_a_after.wkb == ba.geometry_a_after.wkb
        assert ab.geometry_b_after.wkb == ba.geometry_b_after.wkb
        assert ab.centerline_geometry.wkb == ba.centerline_geometry.wkb

    def test_critical_overlap_above_threshold_raises_value_error(self):
        # 5% overlap (500 m² on 10,000 m² parcels) exceeds default 2.0% threshold
        west = _rect(0.0, 0.0, 100.0, 100.0)
        east = _rect(95.0, 0.0, 195.0, 100.0)

        with pytest.raises(ValueError, match="CRITICAL_DISPUTE"):
            resolve_pair_micro_overlap(ID_A, west, ID_B, east)

    def test_asymmetric_overlap_exceeding_threshold_on_smaller_parcel_raises(self):
        # Large parcel: 100x100 = 10,000 m²; small parcel: 10x10 = 100 m²
        # Overlap: 1x10 = 10 m² -> 0.1% of large parcel, 10.0% of small parcel (> 2.0%)
        large = _rect(0.0, 0.0, 100.0, 100.0)
        small = _rect(99.0, 0.0, 109.0, 10.0)

        with pytest.raises(ValueError, match="CRITICAL_DISPUTE"):
            resolve_pair_micro_overlap(ID_A, large, ID_B, small)

    def test_non_overlapping_pair_raises_value_error(self):
        west = _rect(0.0, 0.0, 100.0, 100.0)
        east = _rect(100.0, 0.0, 200.0, 100.0)

        with pytest.raises(ValueError, match="no polygonal overlap"):
            resolve_pair_micro_overlap(ID_A, west, ID_B, east)


class TestBatchMicroOverlapHealing:
    """Batch micro-overlap healing across candidate parcels (`heal_micro_overlaps`)."""

    def test_heals_micro_overlap_and_skips_critical_dispute_pair(self):
        # A and B have a 1% micro-overlap; B and C have a 10% critical overlap
        poly_a = _rect(0.0, 0.0, 100.0, 100.0)
        poly_b = _rect(99.0, 0.0, 199.0, 100.0)
        poly_c = _rect(189.0, 0.0, 289.0, 100.0)  # overlaps B by 10x100 = 1,000 m² (10%)

        result = heal_micro_overlaps([_cand(ID_C, poly_c), _cand(ID_A, poly_a), _cand(ID_B, poly_b)])

        assert isinstance(result, MicroOverlapBatchResult)
        assert len(result.resolved_pairs) == 1
        assert result.resolved_pairs[0].parcel_a_id == ID_A
        assert result.resolved_pairs[0].parcel_b_id == ID_B
        assert result.skipped_critical_pairs == ((ID_B, ID_C),)
        assert ID_A in result.updated_geometries
        assert ID_B in result.updated_geometries
        assert ID_C not in result.updated_geometries
        assert result.updated_metrics[ID_A].area_sqm == pytest.approx(9_950.0, abs=1e-6)

    def test_dispute_locked_parcel_is_skipped_even_when_overlap_is_under_two_percent(self):
        poly_a = _rect(0.0, 0.0, 100.0, 100.0)
        poly_b = _rect(99.0, 0.0, 199.0, 100.0)

        result = heal_micro_overlaps([_cand(ID_A, poly_a, locked=True), _cand(ID_B, poly_b)])

        assert result.resolved_pairs == ()
        assert result.skipped_critical_pairs == ((ID_A, ID_B),)
        assert result.updated_geometries == {}
        assert result.updated_metrics == {}


def _orm_parcel(layer_id: uuid.UUID, pid: uuid.UUID, ident: str, poly: Polygon, *, locked: bool = False) -> HarmonizedParcel:
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
    )


class TestLayerMicroOverlapHealingOnORM:
    """In-place ORM healing (`heal_layer_micro_overlaps`)."""

    def test_updates_projected_and_wgs84_geometries_and_marks_auto_resolved(self):
        layer_id = uuid.uuid4()
        dx, dy = 500_000.0, 3_000_000.0
        p_a = _orm_parcel(layer_id, ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, dx, dy))
        p_b = _orm_parcel(layer_id, ID_B, "B", _rect(99.0, 0.0, 199.0, 100.0, dx, dy))

        result = heal_layer_micro_overlaps([p_b, p_a])

        assert len(result.resolved_pairs) == 1
        geom_a = to_shape(p_a.geometry_projected)
        geom_b = to_shape(p_b.geometry_projected)
        assert extract_polygonal_intersection(geom_a, geom_b).area == pytest.approx(0.0, abs=1e-6)
        assert p_a.metric_area_sqm == pytest.approx(9_950.0, abs=1e-3)
        assert p_b.metric_area_sqm == pytest.approx(9_950.0, abs=1e-3)
        assert p_a.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert p_b.resolution_status == ParcelResolutionStatus.AUTO_RESOLVED.value
        assert to_shape(p_a.geometry_wgs84).geom_type == "MultiPolygon"
        assert to_shape(p_b.geometry_wgs84).geom_type == "MultiPolygon"

    def test_critical_dispute_orm_parcels_remain_unmodified(self):
        layer_id = uuid.uuid4()
        dx, dy = 500_000.0, 3_000_000.0
        p_a = _orm_parcel(layer_id, ID_A, "A", _rect(0.0, 0.0, 100.0, 100.0, dx, dy))
        p_b = _orm_parcel(layer_id, ID_B, "B", _rect(90.0, 0.0, 190.0, 100.0, dx, dy))  # 10% overlap
        a_wkb = bytes(p_a.geometry_projected.data)
        b_wkb = bytes(p_b.geometry_projected.data)

        result = heal_layer_micro_overlaps([p_a, p_b])

        assert result.resolved_pairs == ()
        assert result.skipped_critical_pairs == ((ID_A, ID_B),)
        assert bytes(p_a.geometry_projected.data) == a_wkb
        assert bytes(p_b.geometry_projected.data) == b_wkb
        assert p_a.resolution_status == ParcelResolutionStatus.ORIGINAL.value
        assert p_b.resolution_status == ParcelResolutionStatus.ORIGINAL.value
