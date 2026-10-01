"""Cadastral fixtures package providing deterministic ground truth datasets."""

from fixtures.cadastral_fixtures import (
    EVEREST_1830_CRS,
    UTM_ZONE_43N_CRS,
    WGS84_GEO_CRS,
    export_fixtures_to_geojson,
    export_fixtures_to_wkt_mapping,
    generate_clean_cadastral_fixtures,
    get_fixture_bounds,
    save_fixtures_geojson,
)

__all__ = [
    "EVEREST_1830_CRS",
    "UTM_ZONE_43N_CRS",
    "WGS84_GEO_CRS",
    "generate_clean_cadastral_fixtures",
    "get_fixture_bounds",
    "export_fixtures_to_geojson",
    "export_fixtures_to_wkt_mapping",
    "save_fixtures_geojson",
]
