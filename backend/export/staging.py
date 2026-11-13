"""MinIO staging and presigned download URL generation for harmonized exports."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from backend.core.config import get_settings
from backend.export.flatgeobuf import export_parcels_to_flatgeobuf
from backend.export.geojson import export_parcels_to_geojson_bytes
from backend.export.geopackage import export_parcels_to_geopackage
from backend.models.parcel import HarmonizedParcel
from backend.storage.minio_client import MinioStorageClient, get_storage_client

SUPPORTED_EXPORT_FORMATS: Dict[str, Dict[str, str]] = {
    "geojson": {
        "ext": "geojson",
        "content_type": "application/geo+json",
    },
    "gpkg": {
        "ext": "gpkg",
        "content_type": "application/geopackage+sqlite3",
    },
    "fgb": {
        "ext": "fgb",
        "content_type": "application/vnd.flatgeobuf",
    },
}


@dataclass(frozen=True)
class StagedExportArtifact:
    """Metadata and temporary presigned URL for a staged harmonized export."""

    format: str
    bucket: str
    object_name: str
    download_url: str
    content_type: str
    byte_size: int
    sha256: str
    feature_count: int
    expires_seconds: int
    cached: bool = False


def _sanitize_layer_slug(layer_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", layer_name.strip()).strip("_").lower()
    return slug or "harmonized_layer"


def build_export_object_name(
    layer_id: uuid.UUID,
    layer_name: str,
    export_format: str,
    *,
    srid: int = 4326,
) -> str:
    """Build deterministic MinIO object key for a harmonized layer export."""
    fmt = export_format.lower().strip()
    if fmt not in SUPPORTED_EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format: {export_format!r}")
    slug = _sanitize_layer_slug(layer_name)
    srid_suffix = f"_epsg{srid}" if fmt == "gpkg" and srid != 4326 else ""
    return f"exports/{layer_id}/{slug}{srid_suffix}_harmonized.{SUPPORTED_EXPORT_FORMATS[fmt]['ext']}"


def _is_active_parcel(parcel: HarmonizedParcel, include_merged_slivers: bool) -> bool:
    if include_merged_slivers:
        return True
    attrs = getattr(parcel, "attributes", None) or {}
    return not bool(attrs.get("merged_into_parcel_id"))


def _serialize_export_bytes(
    parcels: Sequence[HarmonizedParcel],
    export_format: str,
    *,
    layer_name: str,
    srid: int,
    include_merged_slivers: bool,
) -> tuple[bytes, int]:
    active_count = sum(1 for p in parcels if _is_active_parcel(p, include_merged_slivers))

    if export_format == "geojson":
        payload = export_parcels_to_geojson_bytes(
            parcels,
            include_merged_slivers=include_merged_slivers,
        )
        return payload, active_count

    if export_format == "gpkg":
        payload = export_parcels_to_geopackage(
            parcels,
            table_name=_sanitize_layer_slug(layer_name),
            srid=srid,
            include_merged_slivers=include_merged_slivers,
        )
        return payload, active_count

    if export_format == "fgb":
        payload = export_parcels_to_flatgeobuf(
            parcels,
            layer_name=_sanitize_layer_slug(layer_name),
            include_merged_slivers=include_merged_slivers,
        )
        return payload, active_count

    raise ValueError(f"Unsupported export format: {export_format!r}")


def stage_harmonized_export(
    *,
    layer_id: uuid.UUID,
    layer_name: str,
    parcels: Sequence[HarmonizedParcel],
    export_format: str,
    storage_client: Optional[MinioStorageClient] = None,
    srid: int = 4326,
    include_merged_slivers: bool = False,
    expires_seconds: int = 3600,
    force_rebuild: bool = False,
) -> StagedExportArtifact:
    """Serialize, upload (unless cached), and issue a presigned download URL."""
    fmt = export_format.lower().strip()
    if fmt not in SUPPORTED_EXPORT_FORMATS:
        raise ValueError(
            f"Unsupported export format: {export_format!r}. "
            f"Expected one of {sorted(SUPPORTED_EXPORT_FORMATS)}."
        )
    if expires_seconds <= 0:
        raise ValueError("expires_seconds must be a positive integer.")

    settings = get_settings()
    bucket = settings.minio_bucket_harmonized
    content_type = SUPPORTED_EXPORT_FORMATS[fmt]["content_type"]
    object_name = build_export_object_name(layer_id, layer_name, fmt, srid=srid)
    client: Any = storage_client or get_storage_client()
    active_count = sum(1 for p in parcels if _is_active_parcel(p, include_merged_slivers))

    if not force_rebuild and client.object_exists(bucket, object_name):
        stat = client.stat_object(bucket, object_name) or {}
        meta = stat.get("metadata") or {}
        sha256 = str(meta.get("x-amz-meta-sha256") or meta.get("sha256") or "")
        byte_size = int(stat.get("size") or 0)
        download_url = client.get_presigned_download_url(
            bucket_name=bucket,
            object_name=object_name,
            expires_seconds=expires_seconds,
        )
        return StagedExportArtifact(
            format=fmt,
            bucket=bucket,
            object_name=object_name,
            download_url=download_url,
            content_type=content_type,
            byte_size=byte_size,
            sha256=sha256,
            feature_count=active_count,
            expires_seconds=expires_seconds,
            cached=True,
        )

    payload, feature_count = _serialize_export_bytes(
        parcels,
        fmt,
        layer_name=layer_name,
        srid=srid,
        include_merged_slivers=include_merged_slivers,
    )
    sha256 = hashlib.sha256(payload).hexdigest()

    client.upload_file(
        bucket_name=bucket,
        object_name=object_name,
        file_data=payload,
        content_type=content_type,
        metadata={
            "layer_id": str(layer_id),
            "format": fmt,
            "sha256": sha256,
            "feature_count": str(feature_count),
        },
    )

    download_url = client.get_presigned_download_url(
        bucket_name=bucket,
        object_name=object_name,
        expires_seconds=expires_seconds,
    )

    return StagedExportArtifact(
        format=fmt,
        bucket=bucket,
        object_name=object_name,
        download_url=download_url,
        content_type=content_type,
        byte_size=len(payload),
        sha256=sha256,
        feature_count=feature_count,
        expires_seconds=expires_seconds,
        cached=False,
    )
