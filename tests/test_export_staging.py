"""Red-phase tests for MinIO harmonized export staging & presigned URLs (Step 113/150)."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import MagicMock

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.core.config import get_settings
from backend.export.staging import (
    SUPPORTED_EXPORT_FORMATS,
    StagedExportArtifact,
    stage_harmonized_export,
)
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus


def _make_parcel(
    layer_id: uuid.UUID,
    parcel_id: str,
    coords: list[tuple[float, float]],
    *,
    is_merged: bool = False,
) -> HarmonizedParcel:
    poly = Polygon(coords)
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=uuid.uuid4(),
        layer_id=layer_id,
        parcel_identifier=parcel_id,
        geometry_wgs84=from_shape(mp, srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=1000.0,
        metric_perimeter_m=120.0,
        polsby_popper_compactness=0.78,
        is_sliver=is_merged,
        is_disputed=False,
        dispute_locked=False,
        resolution_status=ParcelResolutionStatus.AUTO_RESOLVED.value,
        attributes={"owner": "Govt", "ward": 4, **({"merged_into_parcel_id": "P-100"} if is_merged else {})},
    )


class TestHarmonizedExportStaging:
    """Tests for staging harmonized exports to MinIO and issuing presigned URLs."""

    def test_stages_geojson_gpkg_and_fgb_to_harmonized_bucket_with_checksum(self) -> None:
        layer_id = uuid.uuid4()
        parcels = [
            _make_parcel(
                layer_id,
                "P-101",
                [(77.20, 28.60), (77.201, 28.60), (77.201, 28.601), (77.20, 28.601), (77.20, 28.60)],
            )
        ]

        settings = get_settings()
        for fmt in ("geojson", "gpkg", "fgb"):
            mock_storage = MagicMock()
            mock_storage.object_exists.return_value = False
            mock_storage.get_presigned_download_url.return_value = (
                f"https://minio.local/{settings.minio_bucket_harmonized}/exports/{layer_id}/ward4.{fmt}?sig=abc"
            )

            artifact = stage_harmonized_export(
                layer_id=layer_id,
                layer_name="Ward 4!",
                parcels=parcels,
                export_format=fmt,
                storage_client=mock_storage,
                expires_seconds=900,
                force_rebuild=False,
            )

            assert isinstance(artifact, StagedExportArtifact)
            assert artifact.format == fmt
            assert artifact.bucket == settings.minio_bucket_harmonized
            assert artifact.object_name.startswith(f"exports/{layer_id}/")
            assert artifact.object_name.endswith(f".{fmt}")
            assert artifact.expires_seconds == 900
            assert artifact.feature_count == 1
            assert artifact.byte_size > 0
            assert len(artifact.sha256) == 64
            assert artifact.download_url.startswith("https://minio.local/")

            mock_storage.upload_file.assert_called_once()
            upload_kwargs = mock_storage.upload_file.call_args.kwargs
            uploaded_bytes = upload_kwargs["file_data"]
            assert upload_kwargs["bucket_name"] == settings.minio_bucket_harmonized
            assert upload_kwargs["object_name"] == artifact.object_name
            assert upload_kwargs["content_type"] == SUPPORTED_EXPORT_FORMATS[fmt]["content_type"]
            assert hashlib.sha256(uploaded_bytes).hexdigest() == artifact.sha256
            assert upload_kwargs["metadata"]["layer_id"] == str(layer_id)
            assert upload_kwargs["metadata"]["format"] == fmt

            mock_storage.get_presigned_download_url.assert_called_once_with(
                bucket_name=settings.minio_bucket_harmonized,
                object_name=artifact.object_name,
                expires_seconds=900,
            )

    def test_reuses_existing_staged_object_when_not_forced(self) -> None:
        settings = get_settings()
        layer_id = uuid.uuid4()
        parcels = [
            _make_parcel(
                layer_id,
                "P-1",
                [(77.20, 28.60), (77.201, 28.60), (77.201, 28.601), (77.20, 28.601), (77.20, 28.60)],
            )
        ]

        mock_storage = MagicMock()
        mock_storage.object_exists.return_value = True
        mock_storage.stat_object.return_value = {
            "size": 4096,
            "metadata": {"x-amz-meta-sha256": "deadbeef" * 8},
        }
        mock_storage.get_presigned_download_url.return_value = "https://minio.local/presigned"

        artifact = stage_harmonized_export(
            layer_id=layer_id,
            layer_name="sector_9",
            parcels=parcels,
            export_format="gpkg",
            storage_client=mock_storage,
            force_rebuild=False,
        )

        mock_storage.upload_file.assert_not_called()
        assert artifact.cached is True
        assert artifact.byte_size == 4096
        assert artifact.download_url == "https://minio.local/presigned"

    def test_force_rebuild_reuploads_even_when_object_exists(self) -> None:
        layer_id = uuid.uuid4()
        parcels = [
            _make_parcel(
                layer_id,
                "P-1",
                [(77.20, 28.60), (77.201, 28.60), (77.201, 28.601), (77.20, 28.601), (77.20, 28.60)],
            )
        ]

        mock_storage = MagicMock()
        mock_storage.object_exists.return_value = True
        mock_storage.get_presigned_download_url.return_value = "https://minio.local/presigned-new"

        artifact = stage_harmonized_export(
            layer_id=layer_id,
            layer_name="sector_9",
            parcels=parcels,
            export_format="geojson",
            storage_client=mock_storage,
            force_rebuild=True,
        )

        mock_storage.upload_file.assert_called_once()
        assert artifact.cached is False
        assert artifact.download_url == "https://minio.local/presigned-new"

    def test_rejects_unsupported_format_or_invalid_expiry(self) -> None:
        layer_id = uuid.uuid4()
        mock_storage = MagicMock()

        with pytest.raises(ValueError, match="Unsupported export format"):
            stage_harmonized_export(
                layer_id=layer_id,
                layer_name="sector_9",
                parcels=[],
                export_format="shapefile",
                storage_client=mock_storage,
            )

        with pytest.raises(ValueError, match="expires_seconds"):
            stage_harmonized_export(
                layer_id=layer_id,
                layer_name="sector_9",
                parcels=[],
                export_format="geojson",
                storage_client=mock_storage,
                expires_seconds=0,
            )
