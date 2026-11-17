"""Deterministic multi-format harmonization release bundle (.zip) generator (Step 148/150)."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Sequence

from backend.export.flatgeobuf import export_parcels_to_flatgeobuf
from backend.export.geojson import export_parcels_to_geojson_bytes
from backend.export.geopackage import export_parcels_to_geopackage
from backend.export.manifest import export_audit_manifest_bytes
from backend.models.layer import RawCadastralLayer
from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologicalError

DETERMINISTIC_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def _sanitize_slug(name: str | None, fallback: str) -> str:
    raw = (name or fallback).strip()
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_").lower()
    return slug or "harmonized_layer"


def _write_deterministic_zip_entry(zf: zipfile.ZipFile, filename: str, data: bytes) -> None:
    info = zipfile.ZipInfo(filename=filename, date_time=DETERMINISTIC_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def build_harmonization_release_bundle(
    *,
    layer: RawCadastralLayer,
    parcels: Sequence[HarmonizedParcel],
    topology_errors: Sequence[TopologicalError],
    srid: int = 4326,
    include_merged_slivers: bool = False,
) -> bytes:
    """Package GeoJSON, GeoPackage, FlatGeobuf, and audit manifest into a deterministic ZIP."""
    slug = _sanitize_slug(layer.name, str(layer.id))

    geojson_bytes = export_parcels_to_geojson_bytes(
        parcels,
        include_merged_slivers=include_merged_slivers,
    )
    gpkg_bytes = export_parcels_to_geopackage(
        parcels,
        table_name=slug,
        srid=srid,
        include_merged_slivers=include_merged_slivers,
    )
    fgb_bytes = export_parcels_to_flatgeobuf(
        parcels,
        layer_name=slug,
        include_merged_slivers=include_merged_slivers,
    )

    checksums = {
        "geojson": hashlib.sha256(geojson_bytes).hexdigest(),
        "gpkg": hashlib.sha256(gpkg_bytes).hexdigest(),
        "fgb": hashlib.sha256(fgb_bytes).hexdigest(),
    }
    manifest_bytes = export_audit_manifest_bytes(
        layer=layer,
        parcels=parcels,
        topology_errors=topology_errors,
        artifact_checksums=checksums,
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_deterministic_zip_entry(zf, f"{slug}_harmonized.fgb", fgb_bytes)
        _write_deterministic_zip_entry(zf, f"{slug}_harmonized.geojson", geojson_bytes)
        _write_deterministic_zip_entry(zf, f"{slug}_harmonized.gpkg", gpkg_bytes)
        _write_deterministic_zip_entry(zf, "manifest.json", manifest_bytes)

    return buf.getvalue()
