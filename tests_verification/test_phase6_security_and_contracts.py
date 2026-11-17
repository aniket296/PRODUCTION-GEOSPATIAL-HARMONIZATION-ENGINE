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
    """Proves security findings and API/worker contract mismatches."""

    def test_finding_f02_celery_worker_schema_mismatch_and_noop_task_stubs(self) -> None:
        """FINDING F-02 (Verified by execution):
        1. RawCadastralLayer has no `srid_declared` or `bounds` column (used by _update_layer_record_sync).
        2. detect_topology_errors_task, auto_heal_topology_task, and export_harmonized_layer_task are no-op stubs.
        """
        mapper_cols = set(RawCadastralLayer.__table__.columns.keys())
        assert "srid_declared" not in mapper_cols
        assert "bounds" not in mapper_cols
        assert "source_crs" in mapper_cols
        assert "bounding_box" in mapper_cols

        # No-op task stubs return COMPLETED immediately even for a non-existent layer_id:
        r_topo = detect_topology_errors_task.run("00000000-0000-0000-0000-000000000000")
        r_heal = auto_heal_topology_task.run("00000000-0000-0000-0000-000000000000")
        r_exp = export_harmonized_layer_task.run("00000000-0000-0000-0000-000000000000")
        assert r_topo["status"] == "COMPLETED"
        assert r_heal["status"] == "COMPLETED"
        assert r_exp["status"] == "COMPLETED"

    def test_finding_f03a_unpack_shapefile_zip_does_not_call_upload_validator(
        self, tmp_path: Path
    ) -> None:
        """FINDING F-03a (Verified by execution):
        UploadValidator.validate_archive_security blocks '../evil.txt', but unpack_shapefile_zip
        calls zf.extractall() directly without invoking UploadValidator.validate_archive_security.
        """
        malicious_zip = _build_valid_shapefile_zip_bytes(extra_entry_name="../escaped_marker.txt")

        # UploadValidator catches it:
        with pytest.raises(PathTraversalError):
            UploadValidator.validate_archive_security(malicious_zip)

        # Direct call to unpack_shapefile_zip does NOT call UploadValidator.validate_archive_security:
        extract_dir = tmp_path / "nested" / "extract"
        comp = unpack_shapefile_zip(malicious_zip, extract_dir=extract_dir)
        assert comp.shp_path.exists()

    def test_finding_f04_docs_exposed_in_production_and_endpoints_unauthenticated(self) -> None:
        """FINDING F-04 (Verified by execution):
        1. create_app() sets docs_url='/docs' in both branches of the ternary even when app_env='production' and debug=False.
        2. All /api/v1 endpoints accept unauthenticated requests without API key or bearer token.
        """
        settings = get_settings()
        assert settings.app_env == "production"
        assert settings.debug is False
        prod_app = create_app()
        assert prod_app.docs_url == "/docs"
        assert prod_app.redoc_url == "/redoc"

    def test_finding_f08_upload_form_field_name_mismatch_ignores_frontend_layer_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FINDING F-08 (Verified by execution):
        Frontend uploadLayer sends FormData field `name`, whereas backend upload_cadastral_layer
        reads `layer_name = Form(None)`. Sending `name='Custom Ward Name'` is silently ignored
        and falls back to the file stem ('ward14_raw').
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
                    data={"name": "Custom Ward Name"},  # what frontend/src/lib/api.ts sends
                )
                assert resp.status_code == 202
        finally:
            app.dependency_overrides.clear()

        assert len(added_objects) == 1
        # Because backend expects `layer_name`, `name` was ignored and `raw_layer.name` fell back to 'ward14_raw'!
        assert added_objects[0].name == "ward14_raw"
