"""Geometry normalization utilities."""

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
    "to_multipolygon",
]
