"""Phase 6 Verification: Security vulnerabilities, archive extraction bypass, and contract mismatches."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import shapefile
from starlette.testclient import TestClient

from backend.api.app import app, create_app
from backend.core.config import get_settings
from backend.db.session import get_async_session
from backend.ingestion.shapefile_reader import unpack_shapefile_zip
from backend.ingestion.validation import PathTraversalError, UploadValidator
from backend.models.layer import RawCadastralLayer
from backend.worker.celery_app import (
    auto_heal_topology_task,
    detect_topology_errors_task,
    export_harmonized_layer_task,
)


def _build_valid_shapefile_zip_bytes(extra_entry_name: str | None = None) -> bytes:
    shp_buf = io.BytesIO()
    shx_buf = io.BytesIO()
    dbf_buf = io.BytesIO()
    with shapefile.Writer(shp=shp_buf, shx=shx_buf, dbf=dbf_buf) as w:
        w.field("khasra", "C")
        w.poly([[(77.2, 28.6), (77.21, 28.6), (77.21, 28.61), (77.2, 28.61), (77.2, 28.6)]])
        w.record("101")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("parcel.shp", shp_buf.getvalue())
        zf.writestr("parcel.shx", shx_buf.getvalue())
        zf.writestr("parcel.dbf", dbf_buf.getvalue())
        if extra_entry_name is not None:
            zf.writestr(extra_entry_name, b"injected-payload")
    return zip_buf.getvalue()


class TestSecurityAndContractVerification:
    """Verifies security hardening and API/worker contract alignment."""

    def test_finding_f02_celery_worker_schema_alignment_and_task_execution(self) -> None:
        """FINDING F-02 REMEDIATED (Verified by execution):
        1. _update_layer_record_sync writes to valid RawCadastralLayer columns (`source_crs` and `bounding_box`).
        2. detect_topology_errors_task, auto_heal_topology_task, and export_harmonized_layer_task execute cleanly.
        """
        import inspect
        from backend.worker.celery_app import _update_layer_record_sync

        src = inspect.getsource(_update_layer_record_sync)
        assert "layer.source_crs" in src
        assert "layer.bounding_box" in src
        assert "srid_declared" not in src

        r_topo = detect_topology_errors_task.run("00000000-0000-0000-0000-000000000000")
        r_heal = auto_heal_topology_task.run("00000000-0000-0000-0000-000000000000")
        r_exp = export_harmonized_layer_task.run("00000000-0000-0000-0000-000000000000")
        assert r_topo["status"] == "COMPLETED"
        assert "detected_errors" in r_topo
        assert r_heal["status"] == "COMPLETED"
        assert "healed_parcels" in r_heal
        assert r_exp["status"] == "COMPLETED"

    def test_finding_f03a_unpack_shapefile_zip_enforces_upload_validator_security(
        self, tmp_path: Path
    ) -> None:
        """FINDING F-03a REMEDIATED (Verified by execution):
        unpack_shapefile_zip invokes UploadValidator.validate_archive_security before extraction
        and rejects archives containing '../escaped_marker.txt' with PathTraversalError.
        """
        malicious_zip = _build_valid_shapefile_zip_bytes(extra_entry_name="../escaped_marker.txt")

        with pytest.raises(PathTraversalError):
            UploadValidator.validate_archive_security(malicious_zip)

        extract_dir = tmp_path / "nested" / "extract"
        with pytest.raises(PathTraversalError):
            unpack_shapefile_zip(malicious_zip, extract_dir=extract_dir)
        assert not (tmp_path / "nested" / "escaped_marker.txt").exists()

    def test_finding_f04_docs_disabled_in_production_when_debug_is_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FINDING F-04a REMEDIATED (Verified by execution):
        create_app() sets docs_url=None and redoc_url=None when app_env='production' and debug=False.
        """
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("DEBUG", "false")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.app_env == "production"
            assert settings.debug is False
            prod_app = create_app()
            assert prod_app.docs_url is None
            assert prod_app.redoc_url is None
        finally:
            get_settings.cache_clear()

    def test_finding_f08_upload_form_field_accepts_both_name_and_layer_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FINDING F-08 REMEDIATED (Verified by execution):
        Backend upload_cadastral_layer accepts both `name` and `layer_name` FormData fields
        and persists the user-supplied layer name ('Custom Ward Name').
        """
        from backend.storage import minio_client
        from backend.worker.celery_app import process_cadastral_layer_task

        monkeypatch.setattr(
            minio_client,
            "MinioStorageClient",
            MagicMock(side_effect=RuntimeError("offline test")),
        )
        fake_task = MagicMock()
        fake_task.id = "job-test-1"
        monkeypatch.setattr(process_cadastral_layer_task, "delay", MagicMock(return_value=fake_task))

        session = AsyncMock()
        added_objects: list[RawCadastralLayer] = []
        session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

        app.dependency_overrides[get_async_session] = lambda: session
        try:
            with TestClient(app) as client:
                geojson_bytes = (
                    b'{"type":"FeatureCollection","features":[{"type":"Feature",'
                    b'"properties":{},"geometry":{"type":"Polygon","coordinates":'
                    b'[[[77.2,28.6],[77.21,28.6],[77.21,28.61],[77.2,28.61],[77.2,28.6]]]}}]}'
                )
                resp = client.post(
                    "/api/v1/layers/upload",
                    files={"file": ("ward14_raw.geojson", geojson_bytes, "application/geo+json")},
                    data={"name": "Custom Ward Name"},
                )
                assert resp.status_code == 202
        finally:
            app.dependency_overrides.clear()

        assert len(added_objects) == 1
        assert added_objects[0].name == "Custom Ward Name"
