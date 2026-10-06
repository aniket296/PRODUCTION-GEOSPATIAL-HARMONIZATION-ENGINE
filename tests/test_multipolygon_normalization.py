"""Tests for 2D MultiPolygon normalization."""

import pytest
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon, box

from backend.normalizer import (
    EmptyGeometryError,
    NonPolygonalGeometryError,
    force_2d,
    normalize_all,
    normalize_to_2d_multipolygon,
)


def test_force_2d_strips_z_from_polygon():
    poly = Polygon([(0, 0, 5), (10, 0, 6), (10, 10, 7), (0, 10, 8)])
    assert poly.has_z
    flat = force_2d(poly)
    assert not flat.has_z
    assert flat.area == pytest.approx(100.0)


def test_force_2d_noop_for_planar_and_empty():
    planar = box(0, 0, 1, 1)
    assert force_2d(planar) is planar
    empty = Polygon()
    assert force_2d(empty).is_empty


def test_normalize_drops_z_and_returns_multipolygon():
    poly = Polygon([(0, 0, 9), (4, 0, 9), (4, 4, 9), (0, 4, 9)])
    result = normalize_to_2d_multipolygon(poly)
    assert isinstance(result, MultiPolygon)
    assert not result.has_z
    assert all(len(c) == 2 for part in result.geoms for c in part.exterior.coords)


def test_normalize_orients_exterior_ccw_and_interior_cw():
    cw_shell = [(0, 0), (0, 10), (10, 10), (10, 0)]  # clockwise
    ccw_hole = [(2, 2), (4, 2), (4, 4), (2, 4)]  # counter-clockwise
    result = normalize_to_2d_multipolygon(Polygon(cw_shell, [ccw_hole]))
    part = result.geoms[0]
    assert part.exterior.is_ccw is True
    assert part.interiors[0].is_ccw is False
    assert part.area == pytest.approx(96.0)


def test_orientation_can_be_disabled():
    cw_shell = [(0, 0), (0, 10), (10, 10), (10, 0)]
    result = normalize_to_2d_multipolygon(Polygon(cw_shell), orient_rings=False)
    assert result.geoms[0].exterior.is_ccw is False


def test_parts_sorted_deterministically_regardless_of_input_order():
    a, b, c = box(20, 20, 21, 21), box(0, 0, 1, 1), box(10, 10, 11, 11)
    r1 = normalize_to_2d_multipolygon(MultiPolygon([a, b, c]))
    r2 = normalize_to_2d_multipolygon(MultiPolygon([c, a, b]))
    assert r1.wkb == r2.wkb
    assert [g.bounds[0] for g in r1.geoms] == [0.0, 10.0, 20.0]


def test_sorting_can_be_disabled_to_preserve_input_order():
    a, b = box(20, 20, 21, 21), box(0, 0, 1, 1)
    result = normalize_to_2d_multipolygon(MultiPolygon([a, b]), sort_parts=False)
    assert result.geoms[0].bounds == a.bounds


def test_normalization_is_idempotent():
    messy = MultiPolygon([Polygon([(0, 0, 1), (0, 5, 1), (5, 5, 1), (5, 0, 1)]), box(9, 9, 10, 10)])
    once = normalize_to_2d_multipolygon(messy)
    twice = normalize_to_2d_multipolygon(once)
    assert once.wkb == twice.wkb


def test_zero_area_parts_dropped():
    sliver = Polygon([(0, 0), (5, 0), (10, 0)])  # collinear, zero area
    result = normalize_to_2d_multipolygon(MultiPolygon([box(0, 1, 2, 3), sliver]))
    assert len(result.geoms) == 1
    assert result.area == pytest.approx(4.0)


def test_all_zero_area_raises():
    sliver = Polygon([(0, 0), (5, 0), (10, 0)])
    with pytest.raises(NonPolygonalGeometryError, match="positive area"):
        normalize_to_2d_multipolygon(sliver)


def test_zero_area_retained_when_drop_disabled():
    sliver = Polygon([(0, 0), (5, 0), (10, 0)])
    result = normalize_to_2d_multipolygon(sliver, drop_zero_area=False)
    assert len(result.geoms) == 1


def test_geometry_collection_with_z_normalized():
    gc = GeometryCollection(
        [
            Polygon([(0, 0, 3), (2, 0, 3), (2, 2, 3), (0, 2, 3)]),
            LineString([(0, 0, 1), (5, 5, 1)]),
            Point(1, 1, 1),
        ]
    )
    result = normalize_to_2d_multipolygon(gc)
    assert isinstance(result, MultiPolygon)
    assert len(result.geoms) == 1
    assert not result.has_z


def test_empty_and_non_polygonal_rejected():
    with pytest.raises(EmptyGeometryError):
        normalize_to_2d_multipolygon(None)
    with pytest.raises(EmptyGeometryError):
        normalize_to_2d_multipolygon(Polygon())
    with pytest.raises(NonPolygonalGeometryError):
        normalize_to_2d_multipolygon(LineString([(0, 0), (1, 1)]))


def test_normalize_does_not_mutate_input():
    poly = Polygon([(0, 0, 4), (0, 3, 4), (3, 3, 4), (3, 0, 4)])
    before = poly.wkt
    normalize_to_2d_multipolygon(poly)
    assert poly.wkt == before


def test_normalize_all_preserves_input_order_and_type():
    inputs = [box(5, 5, 6, 6), Polygon([(0, 0, 1), (1, 0, 1), (1, 1, 1)]), MultiPolygon([box(9, 9, 10, 10)])]
    results = normalize_all(inputs)
    assert all(isinstance(r, MultiPolygon) for r in results)
    assert results[0].bounds == (5.0, 5.0, 6.0, 6.0)
    assert results[2].bounds == (9.0, 9.0, 10.0, 10.0)


def test_normalize_all_forwards_options():
    cw = Polygon([(0, 0), (0, 4), (4, 4), (4, 0)])
    (result,) = normalize_all([cw], orient_rings=False)
    assert result.geoms[0].exterior.is_ccw is False
