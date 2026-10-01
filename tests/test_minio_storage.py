"""Tests for MinIO object storage client: upload, streaming download, presigned URLs."""

import io
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from minio.error import S3Error
from urllib3.response import HTTPResponse

from backend.storage.minio_client import MinioStorageClient


@pytest.fixture
def mock_minio():
    """Mock minio.Minio SDK client."""
    return MagicMock()


@pytest.fixture
def storage_client(mock_minio):
    """MinioStorageClient initialized with mocked Minio client."""
    return MinioStorageClient(client=mock_minio)


def test_minio_client_defaults_from_settings():
    client = MinioStorageClient(
        endpoint="play.min.io:9000",
        access_key="test_user",
        secret_key="test_pass",
        secure=True,
    )
    assert client.endpoint == "play.min.io:9000"
    assert client.access_key == "test_user"
    assert client.secret_key == "test_pass"
    assert client.secure is True


def test_ensure_bucket_exists_when_missing(storage_client, mock_minio):
    mock_minio.bucket_exists.return_value = False

    created = storage_client.ensure_bucket_exists("cadastral-raw")

    assert created is True
    mock_minio.bucket_exists.assert_called_once_with("cadastral-raw")
    mock_minio.make_bucket.assert_called_once_with("cadastral-raw")


def test_ensure_bucket_exists_when_already_exists(storage_client, mock_minio):
    mock_minio.bucket_exists.return_value = True

    created = storage_client.ensure_bucket_exists("cadastral-raw")

    assert created is False
    mock_minio.bucket_exists.assert_called_once_with("cadastral-raw")
    mock_minio.make_bucket.assert_not_called()


def test_upload_file_bytes(storage_client, mock_minio):
    mock_minio.bucket_exists.return_value = True
    payload = b"test cadastral geojson content"

    result = storage_client.upload_file(
        bucket_name="cadastral-raw",
        object_name="layers/parcel_01.geojson",
        file_data=payload,
        content_type="application/geo+json",
        metadata={"crs": "EPSG:32643"},
    )

    assert result == "layers/parcel_01.geojson"
    mock_minio.put_object.assert_called_once()
    kwargs = mock_minio.put_object.call_args.kwargs
    assert kwargs["bucket_name"] == "cadastral-raw"
    assert kwargs["object_name"] == "layers/parcel_01.geojson"
    assert kwargs["length"] == len(payload)
    assert kwargs["content_type"] == "application/geo+json"
    assert kwargs["metadata"] == {"crs": "EPSG:32643"}


def test_upload_file_binary_stream(storage_client, mock_minio):
    mock_minio.bucket_exists.return_value = True
    stream = io.BytesIO(b"binary dxf cadastre stream")

    result = storage_client.upload_file(
        bucket_name="cadastral-raw",
        object_name="layers/survey.dxf",
        file_data=stream,
        content_type="application/dxf",
    )

    assert result == "layers/survey.dxf"
    mock_minio.put_object.assert_called_once()
    kwargs = mock_minio.put_object.call_args.kwargs
    assert kwargs["length"] == len(b"binary dxf cadastre stream")


def test_download_file_bytes(storage_client, mock_minio):
    mock_response = MagicMock()
    mock_response.read.return_value = b"downloaded parcel content"
    mock_minio.get_object.return_value = mock_response

    data = storage_client.download_file("cadastral-harmonized", "parcels/p1.fgb")

    assert data == b"downloaded parcel content"
    mock_minio.get_object.assert_called_once_with("cadastral-harmonized", "parcels/p1.fgb")
    mock_response.close.assert_called_once()
    mock_response.release_conn.assert_called_once()


def test_stream_download(storage_client, mock_minio):
    mock_response = MagicMock()
    chunks = [b"chunk1_", b"chunk2_", b"chunk3"]
    mock_response.stream.return_value = iter(chunks)
    mock_minio.get_object.return_value = mock_response

    streamed = list(storage_client.stream_download("cadastral-raw", "large_dataset.shp", chunk_size=1024))

    assert streamed == chunks
    mock_response.stream.assert_called_once_with(1024)
    mock_response.close.assert_called_once()
    mock_response.release_conn.assert_called_once()


def test_get_presigned_download_url(storage_client, mock_minio):
    mock_minio.presigned_get_object.return_value = "https://minio.local/cadastral-harmonized/p1.fgb?token=xyz"

    url = storage_client.get_presigned_download_url(
        bucket_name="cadastral-harmonized",
        object_name="parcels/p1.fgb",
        expires_seconds=1800,
    )

    assert url == "https://minio.local/cadastral-harmonized/p1.fgb?token=xyz"
    mock_minio.presigned_get_object.assert_called_once_with(
        bucket_name="cadastral-harmonized",
        object_name="parcels/p1.fgb",
        expires=timedelta(seconds=1800),
    )


def test_get_presigned_upload_url(storage_client, mock_minio):
    mock_minio.presigned_put_object.return_value = "https://minio.local/cadastral-raw/upload.dxf?token=abc"

    url = storage_client.get_presigned_upload_url(
        bucket_name="cadastral-raw",
        object_name="raw/upload.dxf",
        expires_seconds=3600,
    )

    assert url == "https://minio.local/cadastral-raw/upload.dxf?token=abc"
    mock_minio.presigned_put_object.assert_called_once_with(
        bucket_name="cadastral-raw",
        object_name="raw/upload.dxf",
        expires=timedelta(seconds=3600),
    )


def test_file_exists_true_and_false(storage_client, mock_minio):
    # Exists
    mock_minio.stat_object.return_value = MagicMock()
    assert storage_client.file_exists("cadastral-raw", "file.dxf") is True

    # Missing file
    mock_minio.stat_object.side_effect = S3Error(
        code="NoSuchKey",
        message="Object not found",
        resource="/cadastral-raw/missing.dxf",
        request_id="req1",
        host_id="host1",
        response=MagicMock(),
    )
    assert storage_client.file_exists("cadastral-raw", "missing.dxf") is False


def test_delete_file(storage_client, mock_minio):
    storage_client.delete_file("cadastral-raw", "to_delete.dxf")
    mock_minio.remove_object.assert_called_once_with("cadastral-raw", "to_delete.dxf")
