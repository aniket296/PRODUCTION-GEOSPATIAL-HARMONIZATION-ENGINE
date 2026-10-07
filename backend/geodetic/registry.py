"""Authoritative Geodetic Coordinate Reference System (CRS) Registry for Indian Cadastral Records.

Supports WGS 84, UTM zones (42N-47N), Everest 1830 ellipsoid, Kalianpur 1975 India zones,
and custom local cadastral village survey engineering reference frames.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import pyproj
from pyproj.crs import CRS
from pyproj.exceptions import CRSError as PyProjCRSError


class CRSRegistryError(Exception):
    """Base exception for coordinate reference system registry errors."""
    pass


class CRSNotFoundError(CRSRegistryError):
    """Raised when an unknown or unresolvable CRS is requested."""
    pass


class CRSValidationError(CRSRegistryError):
    """Raised when a CRS definition is malformed or invalid."""
    pass


class CRSOutOfBoundsError(CRSRegistryError):
    """Raised when coordinates fall outside the valid geographic domain of a CRS."""
    pass


@dataclass(frozen=True)
class EllipsoidDefinition:
    """Geodetic reference ellipsoid parameters."""

    name: str
    semi_major_axis: float  # a (metres)
    semi_minor_axis: float  # b (metres)
    inverse_flattening: float  # 1/f

    @property
    def flattening(self) -> float:
        """Flattening factor f = 1 / (1/f)."""
        return 1.0 / self.inverse_flattening if self.inverse_flattening != 0.0 else 0.0


# Standard Reference Ellipsoids in Indian Cadastral Geodesy
WGS84_ELLIPSOID = EllipsoidDefinition(
    name="WGS 84",
    semi_major_axis=6378137.0,
    semi_minor_axis=6356752.314245,
    inverse_flattening=298.257223563,
)

EVEREST_1830_ELLIPSOID = EllipsoidDefinition(
    name="Everest 1830 (1975 Definition)",
    semi_major_axis=6377299.151,
    semi_minor_axis=6356072.247,
    inverse_flattening=300.8017,
)

GRS80_ELLIPSOID = EllipsoidDefinition(
    name="GRS 1980",
    semi_major_axis=6378137.0,
    semi_minor_axis=6356752.314140,
    inverse_flattening=298.257222101,
)


@dataclass
class CRSRecord:
    """Standardized representation of a Coordinate Reference System."""

    auth_name: str
    auth_code: Union[int, str]
    name: str
    proj_type: str  # GEOGRAPHIC_2D, PROJECTED, COMPOUND, LOCAL
    datum_name: str
    ellipsoid: EllipsoidDefinition
    unit: str  # degree, metre, foot, chain
    area_of_use_name: str
    bounds: Tuple[float, float, float, float]  # (west_lon, south_lat, east_lon, north_lat)
    epsg_code: Optional[int] = None
    proj4: Optional[str] = None
    wkt2: Optional[str] = None
    is_deprecated: bool = False
    aliases: List[str] = field(default_factory=list)

    @property
    def srid(self) -> str:
        """Formatted SRID string, e.g., 'EPSG:4326' or 'SOI:ZONE_IIA'."""
        return f"{self.auth_name}:{self.auth_code}"

    def to_pyproj_crs(self) -> CRS:
        """Construct native pyproj.CRS instance."""
        if self.epsg_code:
            return CRS.from_epsg(self.epsg_code)
        if self.wkt2:
            return CRS.from_wkt(self.wkt2)
        if self.proj4:
            return CRS.from_proj4(self.proj4)
        return CRS.from_user_input(self.srid)

    def to_dict(self) -> Dict[str, Any]:
        """Convert CRS record to dictionary."""
        return {
            "srid": self.srid,
            "auth_name": self.auth_name,
            "auth_code": self.auth_code,
            "epsg_code": self.epsg_code,
            "name": self.name,
            "proj_type": self.proj_type,
            "datum_name": self.datum_name,
            "ellipsoid": {
                "name": self.ellipsoid.name,
                "a": self.ellipsoid.semi_major_axis,
                "b": self.ellipsoid.semi_minor_axis,
                "rf": self.ellipsoid.inverse_flattening,
            },
            "unit": self.unit,
            "area_of_use_name": self.area_of_use_name,
            "bounds": list(self.bounds),
            "is_deprecated": self.is_deprecated,
            "aliases": list(self.aliases),
        }


class CRSRegistry:
    """Master registry and validation service for Indian and international cadastral CRS."""

    _instance: Optional["CRSRegistry"] = None

    def __init__(self) -> None:
        self._registry: Dict[str, CRSRecord] = {}
        self._alias_map: Dict[str, str] = {}
        self._init_standard_records()

    @classmethod
    def get_instance(cls) -> "CRSRegistry":
        """Singleton accessor for CRSRegistry."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _normalize_key(self, identifier: Union[int, str]) -> str:
        """Normalize any CRS identifier into a canonical lookup key."""
        if isinstance(identifier, int):
            return f"EPSG:{identifier}"

        raw = str(identifier).strip()

        # Pure integer string: "4326" -> "EPSG:4326"
        if raw.isdigit():
            return f"EPSG:{raw}"

        # URN format: "urn:ogc:def:crs:EPSG::4326" -> "EPSG:4326"
        urn_match = re.search(r"urn:ogc:def:crs:([A-Za-z]+):[^:]*:([0-9A-Za-z_]+)", raw, re.IGNORECASE)
        if urn_match:
            return f"{urn_match.group(1).upper()}:{urn_match.group(2)}"

        # Standard prefix: "epsg:4326" -> "EPSG:4326"
        if ":" in raw:
            parts = raw.split(":", 1)
            return f"{parts[0].upper()}:{parts[1]}"

        # Canonical aliases
        return raw.upper()

    def register(self, record: CRSRecord) -> None:
        """Register a CRS record into the active catalog."""
        canonical_key = record.srid.upper()
        self._registry[canonical_key] = record

        # Register EPSG code if available
        if record.epsg_code is not None:
            self._alias_map[f"EPSG:{record.epsg_code}"] = canonical_key
            self._alias_map[str(record.epsg_code)] = canonical_key

        # Register name and aliases
        name_key = record.name.upper()
        self._alias_map[name_key] = canonical_key

        for alias in record.aliases:
            self._alias_map[alias.upper()] = canonical_key

    def get(self, identifier: Union[int, str], strict: bool = True) -> Optional[CRSRecord]:
        """Retrieve a registered CRSRecord by code, name, alias, or PROJ/WKT specification.

        Raises:
            CRSNotFoundError: If strict is True and identifier cannot be resolved.
        """
        key = self._normalize_key(identifier)

        # 1. Direct registry lookup
        if key in self._registry:
            return self._registry[key]

        # 2. Alias mapping lookup
        if key in self._alias_map:
            target_key = self._alias_map[key]
            return self._registry[target_key]

        # 3. Dynamic pyproj fallback for standard unlisted EPSG codes
        try:
            pyproj_crs = CRS.from_user_input(identifier)
            record = self._build_record_from_pyproj(pyproj_crs)
            self.register(record)
            return record
        except Exception:
            pass

        if strict:
            raise CRSNotFoundError(f"Coordinate reference system '{identifier}' could not be resolved in registry")
        return None

    def is_valid(self, identifier: Union[int, str]) -> bool:
        """Determine whether the provided identifier maps to a structurally valid CRS."""
        try:
            return self.get(identifier, strict=False) is not None
        except Exception:
            return False

    def validate_bounds(
        self,
        identifier: Union[int, str],
        coordinates: Union[Tuple[float, float], Tuple[float, float, float, float]],
    ) -> bool:
        """Validate whether given coordinate (lon, lat) or bounding box falls within CRS area of use.

        Raises:
            CRSNotFoundError: If CRS cannot be resolved.
            CRSOutOfBoundsError: If coordinates are completely out of CRS validity domain.
        """
        if len(coordinates) not in (2, 4):
            raise ValueError(f"Expected 2-tuple (lon, lat) or 4-tuple (minx, miny, maxx, maxy), got: {coordinates}")

        record = self.get(identifier, strict=True)
        assert record is not None

        west, south, east, north = record.bounds

        # If CRS bounds cover global extent (-180, -90, 180, 90), coordinates are valid
        if west <= -180.0 and east >= 180.0 and south <= -90.0 and north >= 90.0:
            return True

        # Coordinate point (lon, lat)
        if len(coordinates) == 2:
            lon, lat = coordinates
            in_bounds = (west <= lon <= east) and (south <= lat <= north)
            if not in_bounds:
                raise CRSOutOfBoundsError(
                    f"Point ({lon}, {lat}) is outside valid bounds [{west}, {south}, {east}, {north}] for {record.srid}"
                )
            return True

        # Bounding box (min_lon, min_lat, max_lon, max_lat)
        min_lon, min_lat, max_lon, max_lat = coordinates
        # Intersection test: check if rectangles overlap
        intersects = not (max_lon < west or min_lon > east or max_lat < south or min_lat > north)
        if not intersects:
            raise CRSOutOfBoundsError(
                f"Bounding box {coordinates} does not intersect valid bounds [{west}, {south}, {east}, {north}] for {record.srid}"
            )
        return True

    def suggest_utm_zone(self, longitude: float, latitude: float) -> int:
        """Suggest optimal WGS 84 UTM zone EPSG code for a centroid coordinate in India or worldwide.

        Indian longitudinal extent covers zones 42N (66°-72°E) to 47N (96°-102°E).
        """
        if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
            raise ValueError(f"Invalid geographic coordinate: ({longitude}, {latitude})")

        # Standard UTM zone formula: floor((lon + 180) / 6) + 1
        zone_number = int(math.floor((longitude + 180.0) / 6.0)) + 1
        zone_number = max(1, min(60, zone_number))

        # Northern hemisphere (India is entirely in Northern Hemisphere: lat >= 0)
        if latitude >= 0.0:
            return 32600 + zone_number
        else:
            return 32700 + zone_number

    def suggest_india_zone(self, longitude: float, latitude: float) -> Optional[int]:
        """Suggest the historical Survey of India Kalianpur 1975 Lambert Zone (EPSG:24378 - 24383)."""
        # India Zone I: 28°N - 40°N, 68°E - 96°E (North India: J&K, HP, Punjab, Uttarakhand)
        if 28.0 <= latitude <= 40.0 and 68.0 <= longitude <= 98.0:
            return 24378  # Kalianpur 1975 / India zone I
        # India Zone IIa: 20°N - 28°N, 68°E - 82°E (North-West/Central: Rajasthan, Gujarat, MP, UP, Delhi, Maharashtra North)
        elif 20.0 <= latitude < 28.0 and 68.0 <= longitude <= 82.0:
            return 24379  # Kalianpur 1975 / India zone IIa
        # India Zone IIb: 20°N - 28°N, 82°E - 98°E (North-East: Bihar, West Bengal, Assam, Odisha North)
        elif 20.0 <= latitude < 28.0 and 82.0 < longitude <= 98.0:
            return 24380  # Kalianpur 1975 / India zone IIb
        # India Zone IIIa: 12°N - 20°N, 72°E - 80°E (Peninsular West: Maharashtra, Karnataka North, Goa)
        elif 12.0 <= latitude < 20.0 and 72.0 <= longitude <= 80.0:
            return 24381  # Kalianpur 1975 / India zone IIIa
        # India Zone IIIb: 12°N - 20°N, 80°E - 88°E (Peninsular East: Telangana, AP, Odisha South)
        elif 12.0 <= latitude < 20.0 and 80.0 < longitude <= 88.0:
            return 24382  # Kalianpur 1975 / India zone IIIb
        # India Zone IVa: 8°N - 12°N, 74°E - 82°E (Deep South: Tamil Nadu, Kerala, Karnataka South)
        elif 8.0 <= latitude < 12.0 and 74.0 <= longitude <= 82.0:
            return 24383  # Kalianpur 1975 / India zone IVa

        return None

    def list_supported(self) -> List[CRSRecord]:
        """Return all statically and dynamically registered CRS definitions."""
        return list(self._registry.values())

    def _build_record_from_pyproj(self, crs: CRS) -> CRSRecord:
        """Construct a standardized CRSRecord from pyproj.CRS."""
        auth_name = crs.to_authority()[0] if crs.to_authority() else "PROJ"
        auth_code = crs.to_authority()[1] if crs.to_authority() else crs.name

        epsg_code = None
        if auth_name.upper() == "EPSG" and str(auth_code).isdigit():
            epsg_code = int(auth_code)

        area = crs.area_of_use
        bounds = (
            (area.west, area.south, area.east, area.north)
            if area
            else (-180.0, -90.0, 180.0, 90.0)
        )
        area_name = area.name if area else "World"

        ellipsoid = WGS84_ELLIPSOID
        if crs.ellipsoid:
            ellipsoid = EllipsoidDefinition(
                name=crs.ellipsoid.name,
                semi_major_axis=crs.ellipsoid.semi_major_metre,
                semi_minor_axis=crs.ellipsoid.semi_minor_metre,
                inverse_flattening=crs.ellipsoid.inverse_flattening,
            )

        proj_type = "GEOGRAPHIC_2D"
        if crs.is_projected:
            proj_type = "PROJECTED"
        elif crs.is_compound:
            proj_type = "COMPOUND"
        elif crs.is_engineering:
            proj_type = "LOCAL"

        unit = "degree"
        if crs.axis_info:
            unit = crs.axis_info[0].unit_name

        datum_name = crs.datum.name if crs.datum else "Unknown Datum"

        proj4 = None
        if not crs.is_compound:
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    proj4 = crs.to_proj4()
            except Exception:
                pass

        return CRSRecord(
            auth_name=auth_name,
            auth_code=auth_code,
            name=crs.name,
            proj_type=proj_type,
            datum_name=datum_name,
            ellipsoid=ellipsoid,
            unit=unit,
            area_of_use_name=area_name,
            bounds=bounds,
            epsg_code=epsg_code,
            wkt2=crs.to_wkt(),
            proj4=proj4,
        )

    def _init_standard_records(self) -> None:
        """Populate foundational Indian cadastral and global CRS records."""
        # 1. EPSG:4326 - WGS 84 Geographic 2D
        self.register(
            CRSRecord(
                auth_name="EPSG",
                auth_code=4326,
                name="WGS 84",
                proj_type="GEOGRAPHIC_2D",
                datum_name="World Geodetic System 1984",
                ellipsoid=WGS84_ELLIPSOID,
                unit="degree",
                area_of_use_name="World",
                bounds=(-180.0, -90.0, 180.0, 90.0),
                epsg_code=4326,
                aliases=["WGS84", "CRS84", "OGC:CRS84", "GPS"],
            )
        )

        # 2. EPSG:3857 - Web Mercator / Pseudo-Mercator
        self.register(
            CRSRecord(
                auth_name="EPSG",
                auth_code=3857,
                name="WGS 84 / Pseudo-Mercator",
                proj_type="PROJECTED",
                datum_name="World Geodetic System 1984",
                ellipsoid=WGS84_ELLIPSOID,
                unit="metre",
                area_of_use_name="World between 85.06°S and 85.06°N",
                bounds=(-180.0, -85.06, 180.0, 85.06),
                epsg_code=3857,
                aliases=["WEB_MERCATOR", "GOOGLE_MERCATOR", "EPSG:900913"],
            )
        )

        # 3. EPSG:4240 - Indian 1975 Geographic (Everest 1830)
        self.register(
            CRSRecord(
                auth_name="EPSG",
                auth_code=4240,
                name="Indian 1975",
                proj_type="GEOGRAPHIC_2D",
                datum_name="Indian 1975",
                ellipsoid=EVEREST_1830_ELLIPSOID,
                unit="degree",
                area_of_use_name="India and surrounding region",
                bounds=(68.0, 6.0, 98.0, 38.0),
                epsg_code=4240,
                aliases=["KALIANPUR_1975", "EVEREST_1830_GEO"],
            )
        )

        # 4. Indian UTM Zones: EPSG:32642 to EPSG:32647 (WGS 84 UTM zones 42N - 47N)
        utm_zones = [
            (32642, 42, 66.0, 72.0, "Gujarat, Western Rajasthan"),
            (32643, 43, 72.0, 78.0, "Maharashtra, MP, Delhi, Haryana, Punjab, Gujarat East, Karnataka"),
            (32644, 44, 78.0, 84.0, "UP, MP East, Andhra Pradesh, Telangana, Tamil Nadu"),
            (32645, 45, 84.0, 90.0, "Bihar, West Bengal, Odisha, Jharkhand"),
            (32646, 46, 90.0, 96.0, "Assam, Meghalaya, Tripura, Mizoram"),
            (32647, 47, 96.0, 102.0, "Arunachal Pradesh, Nagaland, Manipur"),
        ]
        for epsg, zone_no, west_lon, east_lon, desc in utm_zones:
            self.register(
                CRSRecord(
                    auth_name="EPSG",
                    auth_code=epsg,
                    name=f"WGS 84 / UTM zone {zone_no}N",
                    proj_type="PROJECTED",
                    datum_name="World Geodetic System 1984",
                    ellipsoid=WGS84_ELLIPSOID,
                    unit="metre",
                    area_of_use_name=f"India: {desc}",
                    bounds=(west_lon, 0.0, east_lon, 84.0),
                    epsg_code=epsg,
                    aliases=[f"UTM_{zone_no}N", f"UTM {zone_no}N", f"WGS84_UTM_{zone_no}N"],
                )
            )

        # 5. Historical Survey of India / Kalianpur 1975 Zones (EPSG:24378 to 24383)
        soi_zones = [
            (24378, "I", "India zone I", 68.0, 28.0, 96.0, 40.0),
            (24379, "IIa", "India zone IIa", 68.0, 20.0, 82.0, 28.0),
            (24380, "IIb", "India zone IIb", 82.0, 20.0, 98.0, 28.0),
            (24381, "IIIa", "India zone IIIa", 72.0, 12.0, 80.0, 20.0),
            (24382, "IIIb", "India zone IIIb", 80.0, 12.0, 88.0, 20.0),
            (24383, "IVa", "India zone IVa", 74.0, 8.0, 82.0, 12.0),
        ]
        for epsg, code_suffix, zone_title, w, s, e, n in soi_zones:
            self.register(
                CRSRecord(
                    auth_name="EPSG",
                    auth_code=epsg,
                    name=f"Kalianpur 1975 / {zone_title}",
                    proj_type="PROJECTED",
                    datum_name="Kalianpur 1975",
                    ellipsoid=EVEREST_1830_ELLIPSOID,
                    unit="metre",
                    area_of_use_name=f"India - {zone_title}",
                    bounds=(w, s, e, n),
                    epsg_code=epsg,
                    aliases=[f"SOI_ZONE_{code_suffix.upper()}", f"KALIANPUR_{code_suffix.upper()}"],
                )
            )


# Global singleton helper instance
crs_registry = CRSRegistry.get_instance()
