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

    @property
    def area_sq_deg(self) -> float:
        """Approximate bounding box extent in square degrees."""
        w, s, e, n = self.bounds
        return max(0.0, (e - w) * (n - s))

    def to_wkt(self, version: str = "WKT2_2019") -> str:
        """Export CRS definition to WKT format (default WKT2:2019)."""
        if self.wkt2:
            return self.wkt2
        pyproj_crs = self.to_pyproj_crs()
        return pyproj_crs.to_wkt(version=version)

    def to_proj_json(self) -> Dict[str, Any]:
        """Export CRS definition to PROJ JSON format."""
        pyproj_crs = self.to_pyproj_crs()
        return pyproj_crs.to_json_dict()

    def to_proj4(self) -> Optional[str]:
        """Export CRS to PROJ4 string if convertible."""
        if self.proj4:
            return self.proj4
        try:
            pyproj_crs = self.to_pyproj_crs()
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return pyproj_crs.to_proj4()
        except Exception:
            return None

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
            "area_sq_deg": round(self.area_sq_deg, 4),
            "is_deprecated": self.is_deprecated,
            "aliases": list(self.aliases),
            "wkt2": self.wkt2,
            "proj4": self.proj4,
        }


@dataclass
class LocalCadastralGrid:
    """Local village cadastral engineering coordinate frame definition.

    Used when revenue survey maps are tied to a local village boundary stone (Bhudan / Gadda stone)
    or village center monument rather than a national geodetic network.
    """

    grid_id: str
    name: str
    village_name: str
    district: str
    state: str
    origin_lon: float
    origin_lat: float
    false_easting: float = 0.0
    false_northing: float = 0.0
    scale_factor: float = 1.0
    azimuth_rotation_deg: float = 0.0
    ellipsoid: EllipsoidDefinition = EVEREST_1830_ELLIPSOID
    extent_radius_km: float = 25.0

    def to_crs_record(self) -> CRSRecord:
        """Convert local cadastral grid into a standardized CRSRecord."""
        # Calculate bounding box approx from extent_radius_km (1 deg approx 111 km)
        delta_deg = self.extent_radius_km / 111.0
        w = round(self.origin_lon - delta_deg, 6)
        e = round(self.origin_lon + delta_deg, 6)
        s = round(self.origin_lat - delta_deg, 6)
        n = round(self.origin_lat + delta_deg, 6)

        # Build PROJ string for Transverse Mercator with local origin
        proj4 = (
            f"+proj=tmerc +lat_0={self.origin_lat} +lon_0={self.origin_lon} "
            f"+k_0={self.scale_factor} +x_0={self.false_easting} +y_0={self.false_northing} "
            f"+a={self.ellipsoid.semi_major_axis} +rf={self.ellipsoid.inverse_flattening} +units=m +no_defs"
        )

        return CRSRecord(
            auth_name="LOCAL",
            auth_code=self.grid_id,
            name=self.name,
            proj_type="LOCAL",
            datum_name=f"Local Cadastral Datum ({self.village_name}, {self.district})",
            ellipsoid=self.ellipsoid,
            unit="metre",
            area_of_use_name=f"Village {self.village_name}, {self.district}, {self.state}",
            bounds=(w, s, e, n),
            proj4=proj4,
            aliases=[
                f"GRID_{self.grid_id.upper()}",
                f"{self.village_name.upper()}_GRID",
                f"LOCAL_{self.district.upper()}_{self.grid_id.upper()}",
            ],
        )


# Survey of India Million Sheet (4° x 4°) Base Origins: sheet_no -> (west_lon, south_lat)
MILLION_SHEET_ORIGINS: Dict[int, Tuple[float, float]] = {
    # Lon 68 - 72
    38: (68.0, 32.0), 39: (68.0, 28.0), 40: (68.0, 24.0), 41: (68.0, 20.0), 42: (68.0, 16.0),
    # Lon 72 - 76
    43: (72.0, 32.0), 44: (72.0, 28.0), 45: (72.0, 24.0), 46: (72.0, 20.0), 47: (72.0, 16.0), 48: (72.0, 12.0), 49: (72.0, 8.0),
    # Lon 76 - 80
    52: (76.0, 32.0), 53: (76.0, 28.0), 54: (76.0, 24.0), 55: (76.0, 20.0), 56: (76.0, 16.0), 57: (76.0, 12.0), 58: (76.0, 8.0),
    # Lon 80 - 84
    62: (80.0, 32.0), 63: (80.0, 28.0), 64: (80.0, 24.0), 65: (80.0, 20.0), 66: (80.0, 16.0), 67: (80.0, 12.0), 68: (80.0, 8.0),
    # Lon 84 - 88
    71: (84.0, 28.0), 72: (84.0, 24.0), 73: (84.0, 20.0), 74: (84.0, 16.0),
    # Lon 88 - 92
    77: (88.0, 28.0), 78: (88.0, 24.0), 79: (88.0, 20.0),
    # Lon 92 - 96
    82: (92.0, 28.0), 83: (92.0, 24.0), 84: (92.0, 20.0),
}


# Indian State and Union Territory Cadastral CRS Presets
STATE_CRS_MAPPING: Dict[str, Dict[str, Any]] = {
    "AP": {"name": "Andhra Pradesh", "utm_epsg": 32644, "soi_epsg": 24382},
    "AR": {"name": "Arunachal Pradesh", "utm_epsg": 32646, "soi_epsg": 24380},
    "AS": {"name": "Assam", "utm_epsg": 32646, "soi_epsg": 24380},
    "BR": {"name": "Bihar", "utm_epsg": 32645, "soi_epsg": 24380},
    "CG": {"name": "Chhattisgarh", "utm_epsg": 32644, "soi_epsg": 24380},
    "CH": {"name": "Chandigarh", "utm_epsg": 32643, "soi_epsg": 24378},
    "DL": {"name": "Delhi", "utm_epsg": 32643, "soi_epsg": 24379},
    "GA": {"name": "Goa", "utm_epsg": 32643, "soi_epsg": 24381},
    "GJ": {"name": "Gujarat", "utm_epsg": 32643, "soi_epsg": 24379},
    "HR": {"name": "Haryana", "utm_epsg": 32643, "soi_epsg": 24379},
    "HP": {"name": "Himachal Pradesh", "utm_epsg": 32643, "soi_epsg": 24378},
    "JK": {"name": "Jammu and Kashmir", "utm_epsg": 32643, "soi_epsg": 24378},
    "JH": {"name": "Jharkhand", "utm_epsg": 32645, "soi_epsg": 24380},
    "KA": {"name": "Karnataka", "utm_epsg": 32643, "soi_epsg": 24381},
    "KL": {"name": "Kerala", "utm_epsg": 32643, "soi_epsg": 24383},
    "LA": {"name": "Ladakh", "utm_epsg": 32643, "soi_epsg": 24378},
    "MP": {"name": "Madhya Pradesh", "utm_epsg": 32643, "soi_epsg": 24379},
    "MH": {"name": "Maharashtra", "utm_epsg": 32643, "soi_epsg": 24381},
    "MN": {"name": "Manipur", "utm_epsg": 32646, "soi_epsg": 24380},
    "ML": {"name": "Meghalaya", "utm_epsg": 32646, "soi_epsg": 24380},
    "MZ": {"name": "Mizoram", "utm_epsg": 32646, "soi_epsg": 24380},
    "NL": {"name": "Nagaland", "utm_epsg": 32646, "soi_epsg": 24380},
    "OD": {"name": "Odisha", "utm_epsg": 32645, "soi_epsg": 24380},
    "PB": {"name": "Punjab", "utm_epsg": 32643, "soi_epsg": 24378},
    "PY": {"name": "Puducherry", "utm_epsg": 32644, "soi_epsg": 24383},
    "RJ": {"name": "Rajasthan", "utm_epsg": 32643, "soi_epsg": 24379},
    "SK": {"name": "Sikkim", "utm_epsg": 32645, "soi_epsg": 24380},
    "TN": {"name": "Tamil Nadu", "utm_epsg": 32644, "soi_epsg": 24383},
    "TS": {"name": "Telangana", "utm_epsg": 32644, "soi_epsg": 24382},
    "TR": {"name": "Tripura", "utm_epsg": 32646, "soi_epsg": 24380},
    "UP": {"name": "Uttar Pradesh", "utm_epsg": 32644, "soi_epsg": 24379},
    "UK": {"name": "Uttarakhand", "utm_epsg": 32644, "soi_epsg": 24378},
    "WB": {"name": "West Bengal", "utm_epsg": 32645, "soi_epsg": 24380},
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

    def search(self, query: str, limit: int = 20) -> List[CRSRecord]:
        """Search registered CRS records by keyword across identifiers, names, and aliases."""
        q = query.strip().upper()
        if not q:
            return list(self._registry.values())[:limit]

        # Exact match priority
        exact = self.get(q, strict=False)
        results: List[CRSRecord] = []
        seen = set()

        if exact:
            results.append(exact)
            seen.add(exact.srid)

        for rec in self._registry.values():
            if rec.srid in seen:
                continue
            if (
                q in rec.name.upper()
                or q in rec.srid.upper()
                or q in rec.datum_name.upper()
                or q in rec.area_of_use_name.upper()
                or any(q in alias.upper() for alias in rec.aliases)
            ):
                results.append(rec)
                seen.add(rec.srid)
                if len(results) >= limit:
                    break

        return results

    def find_matching_crs(
        self,
        bbox: Tuple[float, float, float, float],
        proj_type: Optional[str] = None,
    ) -> List[CRSRecord]:
        """Find registered CRS whose bounds contain or overlap query bounding box.

        Sorted by area (tightest / most specific geographic domain first).
        """
        w, s, e, n = bbox
        candidates: List[CRSRecord] = []
        for rec in self._registry.values():
            if proj_type and rec.proj_type.upper() != proj_type.upper():
                continue
            rw, rs, re, rn = rec.bounds
            intersects = not (e < rw or w > re or n < rs or s > rn)
            if intersects:
                candidates.append(rec)

        candidates.sort(key=lambda r: r.area_sq_deg)
        return candidates

    def register_local_cadastral_grid(self, grid: LocalCadastralGrid) -> CRSRecord:
        """Register a village local cadastral engineering grid into the active catalog."""
        rec = grid.to_crs_record()
        self.register(rec)
        return rec

    def get_crs_for_state(self, state_query: str) -> Optional[Dict[str, Any]]:
        """Look up recommended UTM and Kalianpur CRS for an Indian State or Union Territory."""
        q = state_query.strip().upper()
        matched_key = None
        if q in STATE_CRS_MAPPING:
            matched_key = q
        else:
            for code, data in STATE_CRS_MAPPING.items():
                if q == data["name"].upper() or q in data["name"].upper():
                    matched_key = code
                    break

        if not matched_key:
            return None

        state_data = STATE_CRS_MAPPING[matched_key]
        utm_rec = self.get(state_data["utm_epsg"], strict=False)
        soi_rec = self.get(state_data["soi_epsg"], strict=False)

        return {
            "state_code": matched_key,
            "state_name": state_data["name"],
            "recommended_utm_epsg": state_data["utm_epsg"],
            "recommended_soi_epsg": state_data["soi_epsg"],
            "utm_record": utm_rec.to_dict() if utm_rec else None,
            "soi_record": soi_rec.to_dict() if soi_rec else None,
        }

    def parse_soi_toposheet(self, sheet_id: str) -> Dict[str, Any]:
        """Parse Survey of India Toposheet identifier (1:1M, 1:250k, 1:50k) to geographic bounds and CRS.

        Examples: '53' (million sheet), '53H' (degree sheet), '53H/2' (1:50,000 sheet).
        """
        raw = sheet_id.replace(" ", "").upper()
        match = re.match(r"^(\d{2})([A-P])?(?:/(\d{1,2}))?$", raw)
        if not match:
            raise CRSValidationError(
                f"Invalid Survey of India toposheet identifier: '{sheet_id}'. Expected format like '53', '53H', or '53H/2'."
            )

        million_str, degree_letter, sub_str = match.groups()
        million_num = int(million_str)

        if million_num not in MILLION_SHEET_ORIGINS:
            raise CRSValidationError(
                f"Unsupported or out-of-bounds Survey of India million sheet: '{million_num}'"
            )

        base_w, base_s = MILLION_SHEET_ORIGINS[million_num]
        w, s, e, n = base_w, base_s, base_w + 4.0, base_s + 4.0
        scale = "1:1,000,000"

        if degree_letter:
            scale = "1:250,000"
            idx = ord(degree_letter) - ord("A")  # 0 to 15
            # 4 columns (west to east), 4 rows (north to south)
            col = idx // 4
            row = idx % 4
            w = base_w + col * 1.0
            e = w + 1.0
            n = base_s + 4.0 - row * 1.0
            s = n - 1.0

        if sub_str:
            sub_num = int(sub_str)
            if not (1 <= sub_num <= 16):
                raise CRSValidationError(
                    f"Sub-sheet number in toposheet must be between 1 and 16, got: '{sub_num}'"
                )
            scale = "1:50,000"
            sub_idx = sub_num - 1
            sub_row = sub_idx // 4  # North to South (0 to 3)
            sub_col = sub_idx % 4  # West to East (0 to 3)
            deg_w, deg_n = w, n
            w = deg_w + sub_col * 0.25
            e = w + 0.25
            n = deg_n - sub_row * 0.25
            s = n - 0.25

        centroid_lon = round((w + e) / 2.0, 6)
        centroid_lat = round((s + n) / 2.0, 6)
        utm_epsg = self.suggest_utm_zone(centroid_lon, centroid_lat)
        soi_epsg = self.suggest_india_zone(centroid_lon, centroid_lat)

        return {
            "sheet_id": raw,
            "scale": scale,
            "bounds": (round(w, 6), round(s, 6), round(e, 6), round(n, 6)),
            "centroid": (centroid_lon, centroid_lat),
            "suggested_utm_epsg": utm_epsg,
            "suggested_soi_epsg": soi_epsg,
        }


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
