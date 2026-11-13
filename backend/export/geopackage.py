"""OGC GeoPackage (.gpkg) exporter with full cadastral attribute retention (Step 110/150).

Directive Sections 26-28:
- Exports harmonized cadastral parcels into an OGC GeoPackage 1.3 SQLite container (`PRAGMA application_id = 0x47504B47`)
  with standard `gpkg_spatial_ref_sys`, `gpkg_contents`, and `gpkg_geometry_columns` metadata tables.
- Encodes geometries in OGC GeoPackage Binary (GPB) format (`b"GP"` header + SRID + 2D envelope + standard WKB) in
  either canonical WGS 84 (`EPSG:4326`) or projected UTM Zone 43N (`EPSG:32643`).
- Retains all cadastral metrics, resolution/dispute flags, and custom source `attributes` JSON.
- Excludes merged audit slivers (`attributes["merged_into_parcel_id"]`) by default unless `include_merged_slivers=True`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from shapely import wkb as shapely_wkb
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from backend.models.parcel import HarmonizedParcel

GPKG_APPLICATION_ID: int = 0x47504B47
GPKG_USER_VERSION: int = 10300
SUPPORTED_GPKG_SRIDS: frozenset[int] = frozenset({4326, 32643})
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
)
_UTM43N_WKT = (
    'PROJCS["WGS 84 / UTM zone 43N",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",75],'
    'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
    'PARAMETER["false_northing",0],UNIT["metre",1],AUTHORITY["EPSG","32643"]]'
)


@dataclass(frozen=True, slots=True)
class GeoPackageExportMetadata:
    """Metadata describing an exported OGC GeoPackage asset."""

    feature_count: int
    byte_length: int
    srid: int
    table_name: str
    bbox: tuple[float, float, float, float] | None


def _is_merged_sliver(parcel: HarmonizedParcel) -> bool:
    attrs = parcel.attributes or {}
    return bool(attrs.get("merged_into_parcel_id"))


def _to_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    return MultiPolygon(polys)


def _validate_table_name(table_name: str) -> str:
    if not _TABLE_NAME_RE.match(table_name) or table_name.lower().startswith("gpkg_"):
        raise ValueError(f"Invalid GeoPackage table_name: {table_name!r}")
    return table_name


def _validate_srid(srid: int) -> int:
    if srid not in SUPPORTED_GPKG_SRIDS:
        raise ValueError(f"Unsupported srid {srid}; expected one of {sorted(SUPPORTED_GPKG_SRIDS)}")
    return srid


def _encode_gpkg_binary(mp: MultiPolygon, srid: int) -> bytes:
    """Encode a MultiPolygon into OGC GeoPackage Binary (GPB) format with little-endian 2D envelope."""
    min_x, min_y, max_x, max_y = mp.bounds
    # Flags: bit 0 = 1 (little endian), bits 1..3 = 1 (2D [minx, maxx, miny, maxy] envelope) -> 0x03
    header = struct.pack("<2sBBidddd", b"GP", 0, 0x03, srid, min_x, max_x, min_y, max_y)
    return header + shapely_wkb.dumps(mp)


def _decode_gpkg_binary(blob: bytes) -> BaseGeometry:
    if len(blob) < 8 or blob[:2] != b"GP":
        raise ValueError("Invalid GeoPackage binary geometry header")
    flags = blob[3]
    endian = "<" if (flags & 0x01) else ">"
    env_indicator = (flags >> 1) & 0x07
    env_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    env_len = env_sizes.get(env_indicator)
    if env_len is None:
        raise ValueError(f"Unsupported GeoPackage envelope indicator: {env_indicator}")
    wkb_offset = 8 + env_len
    if endian not in ("<", ">"):
        raise ValueError("Unsupported GeoPackage endianness")
    return shapely_wkb.loads(blob[wkb_offset:])


def _init_gpkg_schema(conn: sqlite3.Connection, table_name: str, srid: int, bbox: tuple[float, float, float, float] | None) -> None:
    conn.execute(f"PRAGMA application_id = {GPKG_APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version = {GPKG_USER_VERSION}")

    conn.execute(
        """
        CREATE TABLE gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL PRIMARY KEY,
            organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL,
            description TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO gpkg_spatial_ref_sys (srs_name, srs_id, organization, organization_coordsys_id, definition, description)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
            ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
            ("WGS 84 geodetic", 4326, "EPSG", 4326, _WGS84_WKT, "longitude/latitude coordinates in decimal degrees on WGS 84"),
            ("WGS 84 / UTM zone 43N", 32643, "EPSG", 32643, _UTM43N_WKT, "projected metric coordinates in meters on UTM Zone 43N"),
        ],
    )

    conn.execute(
        """
        CREATE TABLE gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY,
            data_type TEXT NOT NULL,
            identifier TEXT UNIQUE,
            description TEXT DEFAULT '',
            last_change DATETIME NOT NULL,
            min_x DOUBLE,
            min_y DOUBLE,
            max_x DOUBLE,
            max_y DOUBLE,
            srs_id INTEGER,
            CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE gpkg_geometry_columns (
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL,
            m TINYINT NOT NULL,
            CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
            CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
            CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
        )
        """
    )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    min_x, min_y, max_x, max_y = bbox if bbox is not None else (None, None, None, None)

    conn.execute(
        """
        INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change, min_x, min_y, max_x, max_y, srs_id)
        VALUES (?, 'features', ?, 'Harmonized cadastral parcel geometries and attributes', ?, ?, ?, ?, ?, ?)
        """,
        (table_name, table_name, now_iso, min_x, min_y, max_x, max_y, srid),
    )
    conn.execute(
        """
        INSERT INTO gpkg_geometry_columns (table_name, column_name, geometry_type_name, srs_id, z, m)
        VALUES (?, 'geom', 'MULTIPOLYGON', ?, 0, 0)
        """,
        (table_name, srid),
    )

    conn.execute(
        f"""
        CREATE TABLE {table_name} (
            fid INTEGER PRIMARY KEY AUTOINCREMENT,
            geom BLOB NOT NULL,
            parcel_id TEXT NOT NULL,
            parcel_identifier TEXT NOT NULL,
            metric_area_sqm REAL,
            metric_perimeter_m REAL,
            polsby_popper_compactness REAL,
            is_sliver INTEGER NOT NULL,
            is_disputed INTEGER NOT NULL,
            dispute_locked INTEGER NOT NULL,
            resolution_status TEXT NOT NULL,
            attributes_json TEXT
        )
        """
    )


def export_parcels_to_geopackage(
    parcels: Sequence[HarmonizedParcel],
    *,
    table_name: str = "harmonized_parcels",
    srid: int = 4326,
    include_merged_slivers: bool = False,
) -> bytes:
    """Serialize `HarmonizedParcel` records into an OGC GeoPackage (`.gpkg`) byte buffer."""
    valid_table = _validate_table_name(table_name)
    valid_srid = _validate_srid(srid)
    active = sorted(
        (p for p in parcels if include_merged_slivers or not _is_merged_sliver(p)),
        key=lambda p: str(p.id),
    )

    rows: list[tuple[Any, ...]] = []
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    for parcel in active:
        raw_geom = parcel.geometry_wgs84 if valid_srid == 4326 else parcel.geometry_projected
        mp = _to_multipolygon(to_shape(raw_geom))
        b = mp.bounds
        min_x = min(min_x, float(b[0]))
        min_y = min(min_y, float(b[1]))
        max_x = max(max_x, float(b[2]))
        max_y = max(max_y, float(b[3]))

        attrs_json = json.dumps(parcel.attributes, sort_keys=True) if parcel.attributes is not None else None
        rows.append(
            (
                sqlite3.Binary(_encode_gpkg_binary(mp, valid_srid)),
                str(parcel.id),
                parcel.parcel_identifier or "",
                float(parcel.metric_area_sqm) if parcel.metric_area_sqm is not None else None,
                float(parcel.metric_perimeter_m) if parcel.metric_perimeter_m is not None else None,
                float(parcel.polsby_popper_compactness) if parcel.polsby_popper_compactness is not None else None,
                1 if parcel.is_sliver else 0,
                1 if parcel.is_disputed else 0,
                1 if parcel.dispute_locked else 0,
                parcel.resolution_status or "ORIGINAL",
                attrs_json,
            )
        )

    bbox = (min_x, min_y, max_x, max_y) if rows else None

    with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(str(tmp_path))
        try:
            _init_gpkg_schema(conn, valid_table, valid_srid, bbox)
            if rows:
                conn.executemany(
                    f"""
                    INSERT INTO {valid_table} (
                        geom,
                        parcel_id,
                        parcel_identifier,
                        metric_area_sqm,
                        metric_perimeter_m,
                        polsby_popper_compactness,
                        is_sliver,
                        is_disputed,
                        dispute_locked,
                        resolution_status,
                        attributes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.commit()
        finally:
            conn.close()
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def read_geopackage_features(
    gpkg_bytes: bytes,
    *,
    table_name: str | None = None,
) -> list[dict[str, Any]]:
    """Read feature geometries and retained attributes from an OGC GeoPackage byte buffer."""
    with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
        tmp.write(gpkg_bytes)
        tmp_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(str(tmp_path))
        try:
            resolved_table = table_name
            if resolved_table is None:
                row = conn.execute(
                    "SELECT table_name FROM gpkg_contents WHERE data_type = 'features' LIMIT 1"
                ).fetchone()
                if row is None:
                    return []
                resolved_table = row[0]
            _validate_table_name(resolved_table)

            cursor = conn.execute(
                f"""
                SELECT
                    fid,
                    geom,
                    parcel_id,
                    parcel_identifier,
                    metric_area_sqm,
                    metric_perimeter_m,
                    polsby_popper_compactness,
                    is_sliver,
                    is_disputed,
                    dispute_locked,
                    resolution_status,
                    attributes_json
                FROM {resolved_table}
                ORDER BY fid
                """
            )
            features: list[dict[str, Any]] = []
            for rec in cursor.fetchall():
                features.append(
                    {
                        "fid": rec[0],
                        "geometry": _decode_gpkg_binary(bytes(rec[1])),
                        "properties": {
                            "parcel_id": rec[2],
                            "parcel_identifier": rec[3],
                            "metric_area_sqm": rec[4],
                            "metric_perimeter_m": rec[5],
                            "polsby_popper_compactness": rec[6],
                            "is_sliver": bool(rec[7]),
                            "is_disputed": bool(rec[8]),
                            "dispute_locked": bool(rec[9]),
                            "resolution_status": rec[10],
                            "attributes_json": rec[11],
                        },
                    }
                )
            return features
        finally:
            conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def build_geopackage_metadata(
    parcels: Sequence[HarmonizedParcel],
    gpkg_bytes: bytes,
    *,
    table_name: str = "harmonized_parcels",
    srid: int = 4326,
    include_merged_slivers: bool = False,
) -> GeoPackageExportMetadata:
    """Build export metadata summary for a GeoPackage payload."""
    valid_table = _validate_table_name(table_name)
    valid_srid = _validate_srid(srid)
    active = [p for p in parcels if include_merged_slivers or not _is_merged_sliver(p)]

    bbox: tuple[float, float, float, float] | None = None
    if active:
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for parcel in active:
            raw_geom = parcel.geometry_wgs84 if valid_srid == 4326 else parcel.geometry_projected
            b = to_shape(raw_geom).bounds
            min_x = min(min_x, float(b[0]))
            min_y = min(min_y, float(b[1]))
            max_x = max(max_x, float(b[2]))
            max_y = max(max_y, float(b[3]))
        bbox = (min_x, min_y, max_x, max_y)

    return GeoPackageExportMetadata(
        feature_count=len(active),
        byte_length=len(gpkg_bytes),
        srid=valid_srid,
        table_name=valid_table,
        bbox=bbox,
    )
