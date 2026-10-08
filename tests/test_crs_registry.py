"""Unit tests for Geodetic Coordinate Reference System (CRS) Registry and Validation.

Tests Indian cadastral frames (Everest 1830, Kalianpur 1975 zones), WGS 84 UTM zones (42N-47N),
bounding domain verification, UTM zone auto-discovery, and custom cadastral survey registrations.
"""

import pytest

from backend.geodetic import (
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
    crs_registry,
)


class TestEllipsoidDefinitions:
    """Tests for geodetic reference ellipsoid parameters."""

    def test_wgs84_ellipsoid_parameters(self):
        assert WGS84_ELLIPSOID.name == "WGS 84"
        assert WGS84_ELLIPSOID.semi_major_axis == 6378137.0
        assert pytest.approx(WGS84_ELLIPSOID.inverse_flattening, rel=1e-6) == 298.257223563
        assert pytest.approx(WGS84_ELLIPSOID.flattening, rel=1e-6) == 1.0 / 298.257223563

    def test_everest_1830_ellipsoid_parameters(self):
        assert "Everest" in EVEREST_1830_ELLIPSOID.name
        assert EVEREST_1830_ELLIPSOID.semi_major_axis == 6377299.151
        assert pytest.approx(EVEREST_1830_ELLIPSOID.inverse_flattening, rel=1e-6) == 300.8017
        assert EVEREST_1830_ELLIPSOID.flattening > 0

    def test_grs80_ellipsoid_parameters(self):
        assert GRS80_ELLIPSOID.name == "GRS 1980"
        assert GRS80_ELLIPSOID.semi_major_axis == 6378137.0
        assert pytest.approx(GRS80_ELLIPSOID.inverse_flattening, rel=1e-6) == 298.257222101


class TestCRSRegistryLookups:
    """Tests for CRS resolution across multiple input formats and aliases."""

    def test_lookup_wgs84_by_epsg_int(self):
        record = crs_registry.get(4326)
        assert record is not None
        assert record.srid == "EPSG:4326"
        assert record.epsg_code == 4326
        assert record.proj_type == "GEOGRAPHIC_2D"
        assert record.unit == "degree"

    def test_lookup_wgs84_by_string_variants(self):
        assert crs_registry.get("4326").epsg_code == 4326
        assert crs_registry.get("EPSG:4326").epsg_code == 4326
        assert crs_registry.get("epsg:4326").epsg_code == 4326
        assert crs_registry.get("urn:ogc:def:crs:EPSG::4326").epsg_code == 4326

    def test_lookup_by_aliases(self):
        assert crs_registry.get("WGS84").epsg_code == 4326
        assert crs_registry.get("CRS84").epsg_code == 4326
        assert crs_registry.get("WEB_MERCATOR").epsg_code == 3857
        assert crs_registry.get("GOOGLE_MERCATOR").epsg_code == 3857

    def test_lookup_indian_1975_geographic(self):
        record = crs_registry.get(4240)
        assert record is not None
        assert record.name == "Indian 1975"
        assert record.ellipsoid.semi_major_axis == 6377299.151
        assert crs_registry.get("KALIANPUR_1975").epsg_code == 4240

    def test_lookup_indian_utm_zones(self):
        for zone, epsg in [(42, 32642), (43, 32643), (44, 32644), (45, 32645), (46, 32646), (47, 32647)]:
            rec = crs_registry.get(epsg)
            assert rec is not None
            assert rec.epsg_code == epsg
            assert f"UTM zone {zone}N" in rec.name
            alias_rec = crs_registry.get(f"UTM_{zone}N")
            assert alias_rec.epsg_code == epsg

    def test_lookup_kalianpur_1975_india_zones(self):
        zone_map = {
            24378: "SOI_ZONE_I",
            24379: "SOI_ZONE_IIA",
            24380: "SOI_ZONE_IIB",
            24381: "SOI_ZONE_IIIA",
            24382: "SOI_ZONE_IIIB",
            24383: "SOI_ZONE_IVA",
        }
        for epsg, alias in zone_map.items():
            rec = crs_registry.get(epsg)
            assert rec is not None
            assert rec.epsg_code == epsg
            assert rec.ellipsoid.semi_major_axis == 6377299.151
            assert crs_registry.get(alias).epsg_code == epsg

    def test_dynamic_fallback_for_standard_unlisted_epsg(self):
        # EPSG:2154 (RGF93 / Lambert-93 France) is not statically pre-registered
        rec = crs_registry.get(2154)
        assert rec is not None
        assert rec.epsg_code == 2154
        assert rec.proj_type == "PROJECTED"

    def test_unknown_crs_behavior(self):
        with pytest.raises(CRSNotFoundError):
            crs_registry.get("EPSG:999999999", strict=True)

        assert crs_registry.get("EPSG:999999999", strict=False) is None
        assert crs_registry.get("NONEXISTENT_ZONE", strict=False) is None


class TestCRSValidationAndBounds:
    """Tests for validity checks and geographic domain constraints."""

    def test_is_valid_helper(self):
        assert crs_registry.is_valid("EPSG:4326") is True
        assert crs_registry.is_valid(4326) is True
        assert crs_registry.is_valid("EPSG:32643") is True
        assert crs_registry.is_valid("INVALID_CRS_XYZ") is False
        assert crs_registry.is_valid(-1) is False

    def test_validate_bounds_global_crs(self):
        # EPSG:4326 covers full earth
        assert crs_registry.validate_bounds("EPSG:4326", (77.2, 28.6)) is True
        assert crs_registry.validate_bounds("EPSG:4326", (-122.4, 37.7)) is True
        assert crs_registry.validate_bounds("EPSG:4326", (70.0, 10.0, 80.0, 20.0)) is True

    def test_validate_bounds_regional_utm(self):
        # UTM 43N (EPSG:32643): Lon 72.0 to 78.0, Lat 0.0 to 84.0
        # Delhi: Lon 77.2, Lat 28.6 (within bounds)
        assert crs_registry.validate_bounds("EPSG:32643", (77.2, 28.6)) is True

        # Kolkata: Lon 88.36, Lat 22.57 (outside bounds of UTM 43N)
        with pytest.raises(CRSOutOfBoundsError):
            crs_registry.validate_bounds("EPSG:32643", (88.36, 22.57))

    def test_validate_bounds_bounding_box(self):
        # Overlapping bounding box for UTM 43N
        assert crs_registry.validate_bounds("EPSG:32643", (73.0, 15.0, 77.0, 25.0)) is True

        # Completely disjoint bounding box (e.g. Northeast India in Zone 43N)
        with pytest.raises(CRSOutOfBoundsError):
            crs_registry.validate_bounds("EPSG:32643", (92.0, 24.0, 95.0, 27.0))

    def test_validate_bounds_invalid_tuple_length(self):
        with pytest.raises(ValueError):
            crs_registry.validate_bounds("EPSG:4326", (77.2, 28.6, 100.0))  # 3-tuple


class TestZoneSuggestions:
    """Tests for automated zone suggestion algorithms."""

    def test_suggest_utm_zone_indian_cities(self):
        # Ahmedabad (Lon 72.57, Lat 23.02) -> UTM 43N
        assert crs_registry.suggest_utm_zone(72.57, 23.02) == 32643
        # Delhi (Lon 77.20, Lat 28.61) -> UTM 43N
        assert crs_registry.suggest_utm_zone(77.20, 28.61) == 32643
        # Varanasi (Lon 82.97, Lat 25.31) -> UTM 44N
        assert crs_registry.suggest_utm_zone(82.97, 25.31) == 32644
        # Patna (Lon 85.13, Lat 25.59) -> UTM 45N
        assert crs_registry.suggest_utm_zone(85.13, 25.59) == 32645
        # Guwahati (Lon 91.73, Lat 26.14) -> UTM 46N
        assert crs_registry.suggest_utm_zone(91.73, 26.14) == 32646

    def test_suggest_utm_zone_southern_hemisphere(self):
        # Sydney (Lon 151.20, Lat -33.86) -> UTM 56S (32756)
        assert crs_registry.suggest_utm_zone(151.20, -33.86) == 32756

    def test_suggest_utm_zone_invalid_coordinates(self):
        with pytest.raises(ValueError):
            crs_registry.suggest_utm_zone(200.0, 20.0)
        with pytest.raises(ValueError):
            crs_registry.suggest_utm_zone(75.0, 95.0)

    def test_suggest_india_zone_regions(self):
        # Punjab / North India (Lon 75.5, Lat 31.0) -> Zone I (24378)
        assert crs_registry.suggest_india_zone(75.5, 31.0) == 24378
        # Rajasthan / Central-West (Lon 75.0, Lat 26.0) -> Zone IIa (24379)
        assert crs_registry.suggest_india_zone(75.0, 26.0) == 24379
        # Bihar / North-East (Lon 85.0, Lat 25.0) -> Zone IIb (24380)
        assert crs_registry.suggest_india_zone(85.0, 25.0) == 24380
        # Maharashtra / Peninsular West (Lon 74.0, Lat 18.0) -> Zone IIIa (24381)
        assert crs_registry.suggest_india_zone(74.0, 18.0) == 24381
        # Andhra Pradesh / Peninsular East (Lon 82.0, Lat 16.0) -> Zone IIIb (24382)
        assert crs_registry.suggest_india_zone(82.0, 16.0) == 24382
        # Tamil Nadu / Deep South (Lon 78.0, Lat 10.0) -> Zone IVa (24383)
        assert crs_registry.suggest_india_zone(78.0, 10.0) == 24383
        # Foreign location returns None
        assert crs_registry.suggest_india_zone(-0.12, 51.5) is None


class TestCustomCadastralRegistration:
    """Tests for registering and serializing local village survey reference systems."""

    def test_register_custom_village_grid(self):
        local_record = CRSRecord(
            auth_name="LOCAL",
            auth_code="VILLAGE_042_SURVEY",
            name="Village Rampur Local Cadastral Frame",
            proj_type="LOCAL",
            datum_name="Everest 1830 Local Ground",
            ellipsoid=EVEREST_1830_ELLIPSOID,
            unit="metre",
            area_of_use_name="Rampur Cadastral Block",
            bounds=(76.5, 27.8, 76.6, 27.9),
            aliases=["RAMPUR_CADASTRE", "VILLAGE_042"],
        )
        crs_registry.register(local_record)

        retrieved = crs_registry.get("LOCAL:VILLAGE_042_SURVEY")
        assert retrieved is not None
        assert retrieved.name == "Village Rampur Local Cadastral Frame"

        alias_lookup = crs_registry.get("RAMPUR_CADASTRE")
        assert alias_lookup is not None
        assert alias_lookup.srid == "LOCAL:VILLAGE_042_SURVEY"

    def test_to_dict_and_pyproj_conversion(self):
        rec = crs_registry.get(4326)
        data = rec.to_dict()
        assert data["srid"] == "EPSG:4326"
        assert data["auth_name"] == "EPSG"
        assert data["epsg_code"] == 4326
        assert data["unit"] == "degree"
        assert "ellipsoid" in data
        assert data["ellipsoid"]["name"] == "WGS 84"
        assert "area_sq_deg" in data
        assert data["area_sq_deg"] > 0

        pyproj_crs = rec.to_pyproj_crs()
        assert pyproj_crs.to_epsg() == 4326


class TestCRSSerializationFormats:
    """Tests for WKT, PROJ JSON, and PROJ4 string exports."""

    def test_to_wkt_export(self):
        rec = crs_registry.get(4326)
        wkt = rec.to_wkt()
        assert "GEOGCRS" in wkt or "GEODCRS" in wkt
        assert "WGS 84" in wkt

    def test_to_proj_json_export(self):
        rec = crs_registry.get(32643)
        json_dict = rec.to_proj_json()
        assert isinstance(json_dict, dict)
        assert json_dict["type"] == "ProjectedCRS"

    def test_to_proj4_export(self):
        rec = crs_registry.get(4326)
        proj4 = rec.to_proj4()
        assert proj4 is not None
        assert "+proj=longlat" in proj4


class TestLocalCadastralGrid:
    """Tests for village cadastral engineering reference systems."""

    def test_local_cadastral_grid_registration(self):
        from backend.geodetic import LocalCadastralGrid

        grid = LocalCadastralGrid(
            grid_id="VILLAGE_SHIVAJINAGAR_01",
            name="Shivajinagar Cadastral Village Grid",
            village_name="Shivajinagar",
            district="Pune",
            state="Maharashtra",
            origin_lon=73.8567,
            origin_lat=18.5204,
            false_easting=10000.0,
            false_northing=10000.0,
            scale_factor=0.99995,
            extent_radius_km=15.0,
        )

        rec = crs_registry.register_local_cadastral_grid(grid)
        assert rec.srid == "LOCAL:VILLAGE_SHIVAJINAGAR_01"
        assert rec.proj_type == "LOCAL"
        assert rec.ellipsoid.name == "Everest 1830 (1975 Definition)"
        assert "+proj=tmerc" in rec.proj4
        assert "+lat_0=18.5204" in rec.proj4
        assert "+lon_0=73.8567" in rec.proj4

        # Verify lookup by alias
        alias_rec = crs_registry.get("SHIVAJINAGAR_GRID")
        assert alias_rec is not None
        assert alias_rec.srid == "LOCAL:VILLAGE_SHIVAJINAGAR_01"


class TestToposheetParser:
    """Tests for Survey of India 1:1M, 1:250k, and 1:50k toposheet grid parsing."""

    def test_parse_million_sheet(self):
        res = crs_registry.parse_soi_toposheet("53")
        assert res["sheet_id"] == "53"
        assert res["scale"] == "1:1,000,000"
        # Sheet 53 is Lon 76-80, Lat 28-32
        assert res["bounds"] == (76.0, 28.0, 80.0, 32.0)
        assert res["suggested_utm_epsg"] == 32643 or res["suggested_utm_epsg"] == 32644

    def test_parse_degree_sheet_delhi(self):
        # 53H covers Delhi (Lon 77-78, Lat 28-29)
        res = crs_registry.parse_soi_toposheet("53H")
        assert res["sheet_id"] == "53H"
        assert res["scale"] == "1:250,000"
        assert res["bounds"] == (77.0, 28.0, 78.0, 29.0)
        assert res["suggested_utm_epsg"] == 32643
        assert res["suggested_soi_epsg"] == 24378 or res["suggested_soi_epsg"] == 24379

    def test_parse_fifty_thousand_sheet(self):
        # 53H/2 is sub-sheet of 53H
        res = crs_registry.parse_soi_toposheet("53H/2")
        assert res["sheet_id"] == "53H/2"
        assert res["scale"] == "1:50,000"
        w, s, e, n = res["bounds"]
        assert pytest.approx(e - w, rel=1e-5) == 0.25
        assert pytest.approx(n - s, rel=1e-5) == 0.25
        assert 77.0 <= w < e <= 78.0
        assert 28.0 <= s < n <= 29.0

    def test_parse_invalid_toposheet(self):
        with pytest.raises(CRSValidationError):
            crs_registry.parse_soi_toposheet("999XYZ")

        with pytest.raises(CRSValidationError):
            crs_registry.parse_soi_toposheet("53H/20")  # > 16


class TestStateCRSLookup:
    """Tests for Indian State & UT geodetic presets."""

    def test_lookup_state_by_code(self):
        res = crs_registry.get_crs_for_state("MH")
        assert res is not None
        assert res["state_name"] == "Maharashtra"
        assert res["recommended_utm_epsg"] == 32643
        assert res["recommended_soi_epsg"] == 24381

    def test_lookup_state_by_name(self):
        res = crs_registry.get_crs_for_state("Karnataka")
        assert res is not None
        assert res["state_code"] == "KA"
        assert res["recommended_utm_epsg"] == 32643
        assert res["recommended_soi_epsg"] == 24381

    def test_lookup_state_unknown(self):
        assert crs_registry.get_crs_for_state("UNKNOWN_TERRITORY") is None


class TestSpatialEnvelopeMatching:
    """Tests for finding matching CRS for an envelope sorted by domain specificity."""

    def test_find_matching_crs_delhi_envelope(self):
        delhi_bbox = (76.8, 28.4, 77.4, 28.9)
        matches = crs_registry.find_matching_crs(delhi_bbox, proj_type="PROJECTED")
        assert len(matches) > 0
        # The tightest/most specific regional projection should appear before global
        first = matches[0]
        assert "UTM" in first.name or "Kalianpur" in first.name
        # Verify all returned are PROJECTED
        for m in matches:
            assert m.proj_type == "PROJECTED"


class TestCRSSearch:
    """Tests for text search across CRS catalog."""

    def test_search_by_keyword(self):
        results = crs_registry.search("Kalianpur", limit=10)
        assert len(results) > 0
        assert all(
            "kalianpur" in r.name.lower()
            or "kalianpur" in r.datum_name.lower()
            or any("kalianpur" in a.lower() for a in r.aliases)
            for r in results
        )

    def test_search_by_exact_srid(self):
        results = crs_registry.search("EPSG:3857")
        assert len(results) > 0
        assert results[0].srid == "EPSG:3857"


