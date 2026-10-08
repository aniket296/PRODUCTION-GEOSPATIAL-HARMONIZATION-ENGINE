"""Geodetic engineering and coordinate reference system harmonization subsystem."""

from backend.geodetic.registry import (
    CRSRecord,
    CRSRegistry,
    CRSRegistryError,
    CRSNotFoundError,
    CRSValidationError,
    CRSOutOfBoundsError,
    EllipsoidDefinition,
    EVEREST_1830_ELLIPSOID,
    GRS80_ELLIPSOID,
    WGS84_ELLIPSOID,
    LocalCadastralGrid,
    MILLION_SHEET_ORIGINS,
    STATE_CRS_MAPPING,
    crs_registry,
)

__all__ = [
    "CRSRecord",
    "CRSRegistry",
    "CRSRegistryError",
    "CRSNotFoundError",
    "CRSValidationError",
    "CRSOutOfBoundsError",
    "EllipsoidDefinition",
    "EVEREST_1830_ELLIPSOID",
    "GRS80_ELLIPSOID",
    "WGS84_ELLIPSOID",
    "LocalCadastralGrid",
    "MILLION_SHEET_ORIGINS",
    "STATE_CRS_MAPPING",
    "crs_registry",
]
