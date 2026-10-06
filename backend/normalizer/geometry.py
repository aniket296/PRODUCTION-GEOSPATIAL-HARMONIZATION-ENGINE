"""Deterministic 2D MultiPolygon normalization for cadastral parcel geometries."""

from __future__ import annotations

from typing import Iterable, List

import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from backend.normalizer.multipolygon import to_multipolygon


def force_2d(geometry: BaseGeometry) -> BaseGeometry:
    """Drop Z (and M) ordinates, returning a purely planar geometry."""
    if geometry is None or geometry.is_empty:
        return geometry
    if not geometry.has_z:
        return geometry
    return shapely.force_2d(geometry)


def _part_sort_key(polygon: Polygon):
    """Stable ordering key: bounding box first, then coordinate tuple to break ties."""
    return (polygon.bounds, tuple(polygon.exterior.coords))


def normalize_to_2d_multipolygon(
    geometry: BaseGeometry,
    orient_rings: bool = True,
    sort_parts: bool = True,
    drop_zero_area: bool = True,
) -> MultiPolygon:
    """Normalize any polygonal geometry to a canonical 2D MultiPolygon.

    Steps:
        1. Drop Z/M ordinates.
        2. Cast Polygon / GeometryCollection / MultiPolygon to MultiPolygon.
        3. Optionally discard degenerate zero-area parts.
        4. Orient rings (exterior counter-clockwise, interiors clockwise).
        5. Optionally sort parts deterministically so identical inputs yield identical output.

    Raises:
        EmptyGeometryError / NonPolygonalGeometryError: see :func:`to_multipolygon`.
        NonPolygonalGeometryError: all parts were degenerate and dropped.
    """
    from backend.normalizer.multipolygon import NonPolygonalGeometryError

    multipolygon = to_multipolygon(force_2d(geometry))
    parts: List[Polygon] = list(multipolygon.geoms)

    if drop_zero_area:
        parts = [p for p in parts if p.area > 0.0]
        if not parts:
            raise NonPolygonalGeometryError("Geometry has no polygonal component with positive area")

    if orient_rings:
        parts = [orient(p, sign=1.0) for p in parts]

    if sort_parts:
        parts = sorted(parts, key=_part_sort_key)

    return MultiPolygon(parts)


def normalize_all(geometries: Iterable[BaseGeometry], **kwargs) -> List[MultiPolygon]:
    """Normalize every geometry in an iterable, preserving input order."""
    return [normalize_to_2d_multipolygon(g, **kwargs) for g in geometries]
