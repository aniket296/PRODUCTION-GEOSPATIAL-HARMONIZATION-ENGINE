"""MinIO S3-compatible object storage client for cadastral raw and harmonized assets."""

from __future__ import annotations

import io
from datetime import timedelta
from typing import BinaryIO, Dict, Generator, Optional, Union

from minio import Minio
from minio.error import S3Error

from backend.core.config import get_settings


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

    def upload_file(
        self,
        bucket_name: str,
        object_name: str,
        file_data: Union[bytes, BinaryIO],
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload raw or harmonized geospatial file to MinIO bucket."""
        self.ensure_bucket_exists(bucket_name)

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
