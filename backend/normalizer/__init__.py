"""Geometry normalization utilities."""

from backend.normalizer.geometry import (
    force_2d,
    normalize_all,
    normalize_to_2d_multipolygon,
)
from backend.normalizer.multipolygon import (
    EmptyGeometryError,
    GeometryCastError,
    NonPolygonalGeometryError,
    cast_all_to_multipolygon,
    to_multipolygon,
)

__all__ = [
    "EmptyGeometryError",
    "GeometryCastError",
    "NonPolygonalGeometryError",
    "cast_all_to_multipolygon",
    "force_2d",
    "normalize_all",
    "normalize_to_2d_multipolygon",
    "to_multipolygon",
]
