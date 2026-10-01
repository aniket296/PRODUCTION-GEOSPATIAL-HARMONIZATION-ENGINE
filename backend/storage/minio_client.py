"""MinIO S3-compatible object storage client for cadastral raw and harmonized assets."""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO, Dict, Generator, List, Optional, Union

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

from backend.core.config import get_settings

GEOSPATIAL_MIME_TYPES: Dict[str, str] = {
    ".dxf": "application/dxf",
    ".geojson": "application/geo+json",
    ".json": "application/geo+json",
    ".fgb": "application/vnd.flatgeobuf",
    ".shp": "application/x-shapefile",
    ".dbf": "application/x-dbf",
    ".shx": "application/x-shx",
    ".prj": "text/plain",
    ".gpkg": "application/geopackage+sqlite3",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".zip": "application/zip",
}


def detect_content_type(filename: str) -> str:
    """Deterministically detect MIME content-type based on cadastral file extension."""
    ext = Path(filename).suffix.lower()
    return GEOSPATIAL_MIME_TYPES.get(ext, "application/octet-stream")


class MinioStorageClient:
    """Production S3-compatible client wrapper for MinIO object storage."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        secure: Optional[bool] = None,
        client: Optional[Minio] = None,
    ) -> None:
        settings = get_settings()
        self.endpoint = endpoint or settings.minio_endpoint
        self.access_key = access_key or settings.minio_root_user
        self.secret_key = secret_key or settings.minio_root_password
        self.secure = secure if secure is not None else settings.minio_secure

        if client is not None:
            self.client = client
        else:
            self.client = Minio(
                endpoint=self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )

    def ensure_bucket_exists(self, bucket_name: str) -> bool:
        """Check if bucket exists; if not, create it deterministically."""
        if not self.client.bucket_exists(bucket_name):
            self.client.make_bucket(bucket_name)
            return True
        return False

    def initialize_buckets(self) -> Dict[str, bool]:
        """Ensure both raw and harmonized application storage buckets are provisioned."""
        settings = get_settings()
        raw_created = self.ensure_bucket_exists(settings.minio_bucket_raw)
        harmonized_created = self.ensure_bucket_exists(settings.minio_bucket_harmonized)
        return {
            settings.minio_bucket_raw: raw_created,
            settings.minio_bucket_harmonized: harmonized_created,
        }

    def upload_file(
        self,
        bucket_name: str,
        object_name: str,
        file_data: Union[bytes, BinaryIO],
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload raw or harmonized geospatial file to MinIO bucket."""
        self.ensure_bucket_exists(bucket_name)

        if not content_type or content_type == "application/octet-stream":
            content_type = detect_content_type(object_name)

        if isinstance(file_data, bytes):
            data_stream = io.BytesIO(file_data)
            length = len(file_data)
        else:
            data_stream = file_data
            current_pos = data_stream.tell()
            data_stream.seek(0, io.SEEK_END)
            length = data_stream.tell() - current_pos
            data_stream.seek(current_pos)

        self.client.put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            data=data_stream,
            length=length,
            content_type=content_type,
            metadata=metadata,
        )
        return object_name

    def download_file(self, bucket_name: str, object_name: str) -> bytes:
        """Download complete object content into memory as bytes."""
        response = self.client.get_object(bucket_name, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def stream_download(
        self,
        bucket_name: str,
        object_name: str,
        chunk_size: int = 65536,
    ) -> Generator[bytes, None, None]:
        """Stream object content in chunks to prevent memory exhaustion on large datasets."""
        response = self.client.get_object(bucket_name, object_name)
        try:
            for chunk in response.stream(chunk_size):
                yield chunk
        finally:
            response.close()
            response.release_conn()

    def get_presigned_download_url(
        self,
        bucket_name: str,
        object_name: str,
        expires_seconds: int = 3600,
    ) -> str:
        """Generate presigned GET URL for temporary secure direct download."""
        return self.client.presigned_get_object(
            bucket_name=bucket_name,
            object_name=object_name,
            expires=timedelta(seconds=expires_seconds),
        )

    def get_presigned_upload_url(
        self,
        bucket_name: str,
        object_name: str,
        expires_seconds: int = 3600,
    ) -> str:
        """Generate presigned PUT URL for secure direct client uploads."""
        return self.client.presigned_put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            expires=timedelta(seconds=expires_seconds),
        )

    def get_object_metadata(self, bucket_name: str, object_name: str) -> Dict[str, Any]:
        """Retrieve size, etag, content type, and custom metadata for an object."""
        stat = self.client.stat_object(bucket_name, object_name)
        return {
            "object_name": stat.object_name,
            "size": stat.size,
            "etag": stat.etag,
            "content_type": stat.content_type,
            "last_modified": stat.last_modified,
            "metadata": stat.metadata or {},
        }

    def list_objects(
        self,
        bucket_name: str,
        prefix: Optional[str] = None,
        recursive: bool = True,
    ) -> List[Dict[str, Any]]:
        """List objects in specified bucket with optional folder prefix."""
        results: List[Dict[str, Any]] = []
        objects = self.client.list_objects(bucket_name, prefix=prefix, recursive=recursive)
        for obj in objects:
            results.append(
                {
                    "object_name": obj.object_name,
                    "size": obj.size,
                    "etag": obj.etag,
                    "last_modified": obj.last_modified,
                    "is_dir": obj.is_dir,
                }
            )
        return results

    def copy_object(
        self,
        source_bucket: str,
        source_object: str,
        target_bucket: str,
        target_object: str,
    ) -> None:
        """Copy object from source bucket to target bucket."""
        self.ensure_bucket_exists(target_bucket)
        self.client.copy_object(
            target_bucket,
            target_object,
            CopySource(source_bucket, source_object),
        )

    def file_exists(self, bucket_name: str, object_name: str) -> bool:
        """Check whether object exists in bucket."""
        try:
            self.client.stat_object(bucket_name, object_name)
            return True
        except S3Error as err:
            if err.code in ("NoSuchKey", "NoSuchBucket"):
                return False
            raise

    def delete_file(self, bucket_name: str, object_name: str) -> None:
        """Delete object from specified bucket."""
        self.client.remove_object(bucket_name, object_name)


_storage_client_instance: Optional[MinioStorageClient] = None


def get_storage_client() -> MinioStorageClient:
    """Dependency provider returning singleton MinioStorageClient instance."""
    global _storage_client_instance
    if _storage_client_instance is None:
        _storage_client_instance = MinioStorageClient()
    return _storage_client_instance
