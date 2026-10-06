"""Tests for consistent MultiPolygon casting."""

import pytest
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from backend.normalizer import (
    EmptyGeometryError,
    GeometryCastError,
    NonPolygonalGeometryError,
    cast_all_to_multipolygon,
    to_multipolygon,
)


def test_polygon_cast_to_single_part_multipolygon():
    poly = box(0, 0, 10, 10)
    result = to_multipolygon(poly)
    assert isinstance(result, MultiPolygon)
    assert len(result.geoms) == 1
    assert result.geoms[0].equals_exact(poly, 0)
    assert result.area == pytest.approx(100.0)


def test_multipolygon_passes_through_unchanged():
    mp = MultiPolygon([box(0, 0, 1, 1), box(5, 5, 7, 7)])
    result = to_multipolygon(mp)
    assert isinstance(result, MultiPolygon)
    assert len(result.geoms) == 2
    assert result.equals_exact(mp, 0)


def test_polygon_with_hole_preserves_interior_ring():
    shell = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(2, 2), (4, 2), (4, 4), (2, 4)]
    result = to_multipolygon(Polygon(shell, [hole]))
    assert len(result.geoms[0].interiors) == 1
    assert result.area == pytest.approx(100.0 - 4.0)


def test_part_order_is_preserved():
    a, b, c = box(0, 0, 1, 1), box(10, 10, 11, 11), box(20, 20, 21, 21)
    result = to_multipolygon(MultiPolygon([a, b, c]))
    assert [g.bounds for g in result.geoms] == [a.bounds, b.bounds, c.bounds]


def test_geometry_collection_extracts_polygons_and_drops_others():
    gc = GeometryCollection([box(0, 0, 2, 2), Point(5, 5), LineString([(0, 0), (9, 9)]), box(10, 10, 12, 12)])
    result = to_multipolygon(gc)
    assert isinstance(result, MultiPolygon)
    assert len(result.geoms) == 2
    assert result.area == pytest.approx(8.0)


def test_nested_geometry_collection_is_flattened():
    inner = GeometryCollection([box(0, 0, 1, 1), MultiPolygon([box(3, 3, 4, 4), box(6, 6, 7, 7)])])
    outer = GeometryCollection([inner, box(9, 9, 10, 10)])
    result = to_multipolygon(outer)
    assert len(result.geoms) == 4
    assert all(isinstance(g, Polygon) for g in result.geoms)


def test_multipolygon_with_empty_part_drops_empty():
    mp = MultiPolygon([box(0, 0, 1, 1)])
    gc = GeometryCollection([mp, Polygon()])
    result = to_multipolygon(gc)
    assert len(result.geoms) == 1


@pytest.mark.parametrize("empty", [None, Polygon(), MultiPolygon(), GeometryCollection(), Point()])
def test_empty_or_missing_geometry_rejected(empty):
    with pytest.raises(EmptyGeometryError):
        to_multipolygon(empty)


@pytest.mark.parametrize(
    "geometry",
    [
        Point(1, 1),
        LineString([(0, 0), (1, 1)]),
        MultiPoint([(0, 0), (1, 1)]),
        GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])]),
    ],
)
def test_non_polygonal_geometry_rejected(geometry):
    with pytest.raises(NonPolygonalGeometryError, match="no polygonal component"):
        to_multipolygon(geometry)


def test_error_hierarchy():
    assert issubclass(EmptyGeometryError, GeometryCastError)
    assert issubclass(NonPolygonalGeometryError, GeometryCastError)
    assert issubclass(GeometryCastError, ValueError)


def test_cast_is_idempotent():
    once = to_multipolygon(box(0, 0, 3, 3))
    twice = to_multipolygon(once)
    assert once.equals_exact(twice, 0)
    assert isinstance(twice, MultiPolygon)


def test_cast_all_returns_uniform_type_in_order():
    inputs = [box(0, 0, 1, 1), MultiPolygon([box(2, 2, 3, 3), box(4, 4, 5, 5)]), GeometryCollection([box(6, 6, 7, 7)])]
    results = cast_all_to_multipolygon(inputs)
    assert all(isinstance(r, MultiPolygon) for r in results)
    assert [len(r.geoms) for r in results] == [1, 2, 1]


def test_cast_all_propagates_first_failure():
    with pytest.raises(NonPolygonalGeometryError):
        cast_all_to_multipolygon([box(0, 0, 1, 1), Point(0, 0)])


def test_cast_does_not_mutate_input():
    poly = box(0, 0, 5, 5)
    wkt_before = poly.wkt
    to_multipolygon(poly)
    assert poly.wkt == wkt_before
