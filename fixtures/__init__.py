"""Test fixtures package for deterministic cadastral datasets."""

from fixtures.cadastral_fixtures import (
    EVEREST_1830_CRS,
    UTM_ZONE_43N_CRS,
    WGS84_GEO_CRS,
    generate_clean_cadastral_fixtures,
    get_fixture_bounds,
)

__all__ = [
    "EVEREST_1830_CRS",
    "UTM_ZONE_43N_CRS",
    "WGS84_GEO_CRS",
    "generate_clean_cadastral_fixtures",
    "get_fixture_bounds",
]
