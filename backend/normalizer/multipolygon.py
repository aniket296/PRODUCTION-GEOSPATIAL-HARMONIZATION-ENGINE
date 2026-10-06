"""Consistent MultiPolygon casting for cadastral parcel geometries."""

from __future__ import annotations

from typing import Iterable, List

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry


class GeometryCastError(ValueError):
    """Base error for geometries that cannot be cast to a MultiPolygon."""


class EmptyGeometryError(GeometryCastError):
    """Raised when the geometry is None or has no area-bearing content."""


class NonPolygonalGeometryError(GeometryCastError):
    """Raised when the geometry contains no polygonal component."""


def _collect_polygons(geometry: BaseGeometry, parts: List[Polygon]) -> None:
    """Recursively flatten polygonal components, preserving encounter order."""
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        parts.append(geometry)
    elif isinstance(geometry, MultiPolygon):
        parts.extend(p for p in geometry.geoms if not p.is_empty)
    elif isinstance(geometry, GeometryCollection):
        for member in geometry.geoms:
            _collect_polygons(member, parts)


def to_multipolygon(geometry: BaseGeometry) -> MultiPolygon:
    """Cast any polygonal geometry to a MultiPolygon.

    - ``Polygon`` becomes a single-part ``MultiPolygon``.
    - ``MultiPolygon`` is returned with empty parts removed.
    - ``GeometryCollection`` has its polygonal members extracted (nested collections flattened).
    - Non-polygonal members (points, lines) inside a collection are discarded.

    Raises:
        EmptyGeometryError: geometry is ``None`` or empty.
        NonPolygonalGeometryError: no polygonal component exists.
    """
    if geometry is None or geometry.is_empty:
        raise EmptyGeometryError("Cannot cast empty or missing geometry to MultiPolygon")

    parts: List[Polygon] = []
    _collect_polygons(geometry, parts)

    if not parts:
        raise NonPolygonalGeometryError(
            f"Geometry of type {geometry.geom_type} has no polygonal component to cast to MultiPolygon"
        )
    return MultiPolygon(parts)


def cast_all_to_multipolygon(geometries: Iterable[BaseGeometry]) -> List[MultiPolygon]:
    """Cast every geometry in an iterable, preserving input order."""
    return [to_multipolygon(g) for g in geometries]
