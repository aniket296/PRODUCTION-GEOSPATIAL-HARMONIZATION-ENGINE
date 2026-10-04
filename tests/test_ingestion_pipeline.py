"""Tests for ingestion task pipeline and file format auto-detection."""

import io
import json
from pathlib import Path
import struct
import uuid
import zipfile

import pytest
from shapely.geometry import Polygon, box
import shapefile

from backend.ingestion.dxf_reader import (
    DXFIngestionReader,
)
from backend.ingestion.flatgeobuf_reader import (
    build_synthetic_fgb_payload,
)
from backend.ingestion.geotiff_reader import (
    SyntheticGeoTIFFPage,
    build_synthetic_geotiff,
)
from backend.ingestion.pipeline import (
    CorruptDataError,
    IngestionPipelineError,
    UnifiedFeatureRecord,
    UnifiedIngestionDispatcher,
    UnifiedIngestionResult,
    UnsupportedFormatError,
    detect_format_from_bytes,
    detect_format_from_file,
)
from backend.models.layer import SupportedFormat
from backend.worker.celery_app import celery_app, process_cadastral_layer_task
from fixtures.cadastral_fixtures import generate_clean_cadastral_fixtures
from fixtures.defect_fixtures import generate_synthetic_dxf_cadastre


EVEREST_1830_PRJ = (
    'PROJCS["Kalianpur 1975 / India Zone IIa",GEOGCS["Kalianpur 1975",'
    'DATUM["Kalianpur_1975",SPHEROID["Everest_1830_1975_Definition",6377299.151,300.8017]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Lambert_Conformal_Conic_2SP"],PARAMETER["False_Easting",3000000.0],'
    'PARAMETER["False_Northing",1000000.0],PARAMETER["Central_Meridian",74.0],'
    'PARAMETER["Standard_Parallel_1",29.0],PARAMETER["Standard_Parallel_2",35.0],'
    'PARAMETER["Latitude_Of_Origin",32.5],UNIT["Metre",1.0],AUTHORITY["EPSG","24378"]]'
)


def _build_sample_geojson_bytes() -> bytes:
    """Helper creating valid GeoJSON FeatureCollection bytes."""
    data = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:EPSG::4326"},
        },
        "features": [
            {
                "type": "Feature",
                "id": "PARCEL-001",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [77.100, 28.600],
                            [77.105, 28.600],
                            [77.105, 28.605],
                            [77.100, 28.605],
                            [77.100, 28.600],
                        ]
                    ],
                },
                "properties": {
                    "khasra_no": "101/1",
                    "owner": "Rajesh Kumar",
                    "area_sqm": 2500.0,
                },
            },
            {
                "type": "Feature",
                "id": "PARCEL-002",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [77.105, 28.600],
                            [77.110, 28.600],
                            [77.110, 28.605],
                            [77.105, 28.605],
                            [77.105, 28.600],
                        ]
                    ],
                },
                "properties": {
                    "khasra_no": "101/2",
                    "owner": "Amit Singh",
                    "area_sqm": 2500.0,
                },
            },
        ],
    }
    return json.dumps(data).encode("utf-8")


def _build_sample_shapefile_zip_bytes(prj_content: str = EVEREST_1830_PRJ) -> bytes:
    """Helper generating in-memory ZIP archive with valid Shapefile files."""
    parcels = generate_clean_cadastral_fixtures()[:3]

    shp_buf = io.BytesIO()
    shx_buf = io.BytesIO()
    dbf_buf = io.BytesIO()

    with shapefile.Writer(shp=shp_buf, shx=shx_buf, dbf=dbf_buf, shapeType=shapefile.POLYGON) as w:
        w.field("khasra_no", "C", size=50)
        w.field("owner", "C", size=100)
        w.field("area_sqm", "N", decimal=2)

        for p in parcels:
            poly: Polygon = p["geometry_everest"]
            coords = [list(pt) for pt in poly.exterior.coords]
            w.poly([coords])
            w.record(
                khasra_no=p["khasra_number"],
                owner=p["owner_name"],
                area_sqm=p["area_sqm"],
            )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("cadastre.shp", shp_buf.getvalue())
        zf.writestr("cadastre.shx", shx_buf.getvalue())
        zf.writestr("cadastre.dbf", dbf_buf.getvalue())
        zf.writestr("cadastre.prj", prj_content)
        zf.writestr("cadastre.cpg", "UTF-8")

    return zip_buf.getvalue()


def _build_sample_dxf_bytes() -> bytes:
    """Helper generating synthetic DXF CAD cadastral drawing bytes."""
    return generate_synthetic_dxf_cadastre().encode("utf-8")


def _build_sample_flatgeobuf_bytes() -> bytes:
    """Helper generating synthetic FlatGeobuf payload bytes."""
    return build_synthetic_fgb_payload(
        name="kalianpur_cadastre",
        features_count=5,
        index_node_size=16,
        crs_code=24378,
        crs_name="EPSG:24378",
        envelope=(3600000.0, 500000.0, 3600500.0, 500300.0),
        columns=[("khasra_no", 1), ("owner_name", 1), ("area_sqm", 2)],
    )


def _build_sample_geotiff_bytes() -> bytes:
    """Helper generating synthetic GeoTIFF raster bytes."""
    spec = SyntheticGeoTIFFPage(
        width=128,
        height=128,
        tiepoints=[(0.0, 0.0, 0.0, 3600000.0, 500000.0, 0.0)],
        pixel_scale=(0.5, 0.5, 0.0),
        epsg=24378,
    )
    return build_synthetic_geotiff([spec])


# ---------------------------------------------------------------------------
# Format Auto-Detection Tests
# ---------------------------------------------------------------------------


def test_detect_format_geojson_bytes():
    """Verify auto-detection of GeoJSON from raw bytes and strings."""
    data = _build_sample_geojson_bytes()
    assert detect_format_from_bytes(data) == SupportedFormat.GEOJSON

    # Raw single feature
    single_feat = json.dumps({"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}}).encode("utf-8")
    assert detect_format_from_bytes(single_feat) == SupportedFormat.GEOJSON


def test_detect_format_shapefile_zip():
    """Verify auto-detection of Shapefile from ZIP archive containing .shp."""
    zip_bytes = _build_sample_shapefile_zip_bytes()
    assert detect_format_from_bytes(zip_bytes) == SupportedFormat.SHAPEFILE
    assert detect_format_from_bytes(zip_bytes, filename="parcels.zip") == SupportedFormat.SHAPEFILE


def test_detect_format_shapefile_binary_magic():
    """Verify auto-detection of Shapefile from direct .shp file header (0x0000270a)."""
    shp_header = struct.pack(">I", 9994) + b"\x00" * 96
    assert detect_format_from_bytes(shp_header) == SupportedFormat.SHAPEFILE


def test_detect_format_dxf_ascii_and_binary():
    """Verify auto-detection of DXF from ASCII text markers and binary DXF header."""
    dxf_ascii = _build_sample_dxf_bytes()
    assert detect_format_from_bytes(dxf_ascii) == SupportedFormat.DXF

    dxf_binary_header = b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 32
    assert detect_format_from_bytes(dxf_binary_header) == SupportedFormat.DXF


def test_detect_format_flatgeobuf_magic():
    """Verify auto-detection of FlatGeobuf from magic bytes."""
    fgb_bytes = _build_sample_flatgeobuf_bytes()
    assert detect_format_from_bytes(fgb_bytes) == SupportedFormat.FLATGEOBUF


def test_detect_format_geotiff_magic_variants():
    """Verify auto-detection of GeoTIFF across little-endian, big-endian, and BigTIFF headers."""
    # Classic little-endian
    assert detect_format_from_bytes(b"II*\x00\x08\x00\x00\x00") == SupportedFormat.GEOTIFF
    # Classic big-endian
    assert detect_format_from_bytes(b"MM\x00*\x00\x00\x00\x08") == SupportedFormat.GEOTIFF
    # BigTIFF little-endian
    assert detect_format_from_bytes(b"II+\x00\x08\x00\x00\x00") == SupportedFormat.GEOTIFF
    # BigTIFF big-endian
    assert detect_format_from_bytes(b"MM\x00+\x00\x00\x00\x08") == SupportedFormat.GEOTIFF

    # Full synthetic payload
    gt_bytes = _build_sample_geotiff_bytes()
    assert detect_format_from_bytes(gt_bytes) == SupportedFormat.GEOTIFF


def test_detect_format_kmz_and_kml():
    """Verify detection of KML and KMZ formats."""
    kml_bytes = b'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document></Document></kml>'
    assert detect_format_from_bytes(kml_bytes) == SupportedFormat.KML

    # KMZ archive containing doc.kml
    kmz_buf = io.BytesIO()
    with zipfile.ZipFile(kmz_buf, "w") as zf:
        zf.writestr("doc.kml", kml_bytes)
    assert detect_format_from_bytes(kmz_buf.getvalue()) == SupportedFormat.KMZ


def test_detect_format_fallback_filename_extension():
    """Verify fallback to filename extension for recognized geospatial extensions."""
    dummy_payload = b"GENERIC UNSTRUCTURED DATA PAYLOAD"
    assert detect_format_from_bytes(dummy_payload, filename="survey.geojson") == SupportedFormat.GEOJSON
    assert detect_format_from_bytes(dummy_payload, filename="parcels.dxf") == SupportedFormat.DXF
    assert detect_format_from_bytes(dummy_payload, filename="boundary.fgb") == SupportedFormat.FLATGEOBUF
    assert detect_format_from_bytes(dummy_payload, filename="orthophoto.tif") == SupportedFormat.GEOTIFF
    assert detect_format_from_bytes(dummy_payload, filename="orthophoto.tiff") == SupportedFormat.GEOTIFF


def test_detect_format_empty_bytes_raises_error():
    """Verify empty byte payload raises CorruptDataError."""
    with pytest.raises(CorruptDataError, match="0 bytes"):
        detect_format_from_bytes(b"")


def test_detect_format_unknown_data_raises_unsupported_format_error():
    """Verify unidentifiable random payload raises UnsupportedFormatError."""
    random_bytes = b"\xde\xad\xbe\xef\xca\xfe\xba\xbe\x01\x02\x03\x04"
    with pytest.raises(UnsupportedFormatError, match="Unable to auto-detect geospatial format"):
        detect_format_from_bytes(random_bytes)


def test_detect_format_from_file_all_types(tmp_path: Path):
    """Verify detect_format_from_file detects each format on disk."""
    # 1. GeoJSON file
    gj_file = tmp_path / "cadastre.geojson"
    gj_file.write_bytes(_build_sample_geojson_bytes())
    assert detect_format_from_file(gj_file) == SupportedFormat.GEOJSON

    # 2. Shapefile zip
    shp_file = tmp_path / "cadastre.zip"
    shp_file.write_bytes(_build_sample_shapefile_zip_bytes())
    assert detect_format_from_file(shp_file) == SupportedFormat.SHAPEFILE

    # 3. DXF file
    dxf_file = tmp_path / "cadastre.dxf"
    dxf_file.write_bytes(_build_sample_dxf_bytes())
    assert detect_format_from_file(dxf_file) == SupportedFormat.DXF

    # 4. FlatGeobuf file
    fgb_file = tmp_path / "cadastre.fgb"
    fgb_file.write_bytes(_build_sample_flatgeobuf_bytes())
    assert detect_format_from_file(fgb_file) == SupportedFormat.FLATGEOBUF

    # 5. GeoTIFF file
    gt_file = tmp_path / "ortho.tif"
    gt_file.write_bytes(_build_sample_geotiff_bytes())
    assert detect_format_from_file(gt_file) == SupportedFormat.GEOTIFF


def test_detect_format_from_file_directory_with_shapefile(tmp_path: Path):
    """Verify detect_format_from_file recognizes a folder containing .shp files."""
    shp_dir = tmp_path / "shp_folder"
    shp_dir.mkdir()
    (shp_dir / "parcels.shp").write_bytes(struct.pack(">I", 9994) + b"\x00" * 96)
    (shp_dir / "parcels.shx").write_bytes(b"\x00" * 100)
    (shp_dir / "parcels.dbf").write_bytes(b"\x00" * 100)

    assert detect_format_from_file(shp_dir) == SupportedFormat.SHAPEFILE


def test_detect_format_from_file_non_existent_and_empty_dir(tmp_path: Path):
    """Verify error cases for missing paths and empty directories."""
    missing = tmp_path / "does_not_exist.geojson"
    with pytest.raises(FileNotFoundError):
        detect_format_from_file(missing)

    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    with pytest.raises(UnsupportedFormatError, match="does not contain supported geospatial"):
        detect_format_from_file(empty_dir)


# ---------------------------------------------------------------------------
# Unified Ingestion Dispatcher Tests
# ---------------------------------------------------------------------------


def test_dispatcher_ingest_bytes_geojson():
    """Verify UnifiedIngestionDispatcher processes GeoJSON bytes into normalized records."""
    data = _build_sample_geojson_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data)

    assert isinstance(result, UnifiedIngestionResult)
    assert result.format == SupportedFormat.GEOJSON
    assert result.detected_crs == "EPSG:4326"
    assert result.feature_count == 2
    assert result.is_raster is False
    assert result.bounds is not None

    for feat in result.features:
        assert isinstance(feat, UnifiedFeatureRecord)
        assert feat.id in ("PARCEL-001", "PARCEL-002")
        assert feat.is_valid is True
        assert feat.crs == "EPSG:4326"
        assert "khasra_no" in feat.properties
        assert "owner" in feat.properties
        assert isinstance(feat.geometry, Polygon)


def test_dispatcher_ingest_bytes_shapefile():
    """Verify UnifiedIngestionDispatcher processes Shapefile ZIP bytes."""
    data = _build_sample_shapefile_zip_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data)

    assert isinstance(result, UnifiedIngestionResult)
    assert result.format == SupportedFormat.SHAPEFILE
    assert result.detected_crs == "EPSG:24378"
    assert result.feature_count == 3
    assert result.is_raster is False
    assert result.bounds is not None

    for feat in result.features:
        assert isinstance(feat, UnifiedFeatureRecord)
        assert feat.is_valid is True
        assert feat.crs == "EPSG:24378"
        assert "khasra_no" in feat.properties
        assert isinstance(feat.geometry, Polygon)


def test_dispatcher_ingest_bytes_dxf():
    """Verify UnifiedIngestionDispatcher processes DXF ASCII bytes."""
    data = _build_sample_dxf_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data)

    assert isinstance(result, UnifiedIngestionResult)
    assert result.format == SupportedFormat.DXF
    assert result.feature_count == 14  # 10 parcels + 4 GCPs
    assert result.is_raster is False
    assert result.bounds is not None
    assert "layers" in result.metadata
    assert "PARCELS" in result.metadata["layers"]

    parcel_features = [f for f in result.features if f.properties.get("layer") == "PARCELS"]
    assert len(parcel_features) == 10
    for feat in parcel_features:
        assert isinstance(feat, UnifiedFeatureRecord)
        assert feat.is_valid is True
        assert isinstance(feat.geometry, Polygon)


def test_dispatcher_ingest_bytes_flatgeobuf():
    """Verify UnifiedIngestionDispatcher processes FlatGeobuf binary payload."""
    data = _build_sample_flatgeobuf_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data)

    assert isinstance(result, UnifiedIngestionResult)
    assert result.format == SupportedFormat.FLATGEOBUF
    assert result.detected_crs == "EPSG:24378"
    assert result.feature_count == 5
    assert result.is_raster is False
    assert result.bounds is not None
    assert result.metadata.get("has_spatial_index") is True

    for feat in result.features:
        assert isinstance(feat, UnifiedFeatureRecord)
        assert feat.is_valid is True
        assert feat.crs == "EPSG:24378"


def test_dispatcher_ingest_bytes_geotiff():
    """Verify UnifiedIngestionDispatcher processes GeoTIFF raster bytes."""
    data = _build_sample_geotiff_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data)

    assert isinstance(result, UnifiedIngestionResult)
    assert result.format == SupportedFormat.GEOTIFF
    assert result.detected_crs == "EPSG:24378"
    assert result.is_raster is True
    assert result.raster_metadata is not None
    assert result.raster_metadata.width == 128
    assert result.raster_metadata.height == 128
    assert result.bounds is not None


def test_dispatcher_ingest_file_roundtrip(tmp_path: Path):
    """Verify UnifiedIngestionDispatcher.ingest_file reads datasets from disk."""
    # GeoJSON file
    gj_path = tmp_path / "parcels.geojson"
    gj_path.write_bytes(_build_sample_geojson_bytes())
    res_gj = UnifiedIngestionDispatcher.ingest_file(gj_path)
    assert res_gj.format == SupportedFormat.GEOJSON
    assert res_gj.feature_count == 2

    # GeoTIFF file
    gt_path = tmp_path / "ortho.tif"
    gt_path.write_bytes(_build_sample_geotiff_bytes())
    res_gt = UnifiedIngestionDispatcher.ingest_file(gt_path)
    assert res_gt.format == SupportedFormat.GEOTIFF
    assert res_gt.is_raster is True


def test_dispatcher_target_crs_override():
    """Verify dispatcher assigns target_crs to dataset and features when requested."""
    data = _build_sample_geojson_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data, target_crs="EPSG:3857")

    assert result.detected_crs == "EPSG:3857"
    for feat in result.features:
        assert feat.crs == "EPSG:3857"
        assert feat.is_valid is True


def test_dispatcher_corrupt_payload_rejection():
    """Verify corrupt data raises CorruptDataError across formats."""
    # Corrupt GeoJSON
    with pytest.raises(CorruptDataError):
        UnifiedIngestionDispatcher.ingest_bytes(b'{"type": "FeatureCollection", "features": [', format_hint=SupportedFormat.GEOJSON)

    # Corrupt DXF
    with pytest.raises(CorruptDataError):
        UnifiedIngestionDispatcher.ingest_bytes(b"0\nSECTION\n2\nENTITIES\n999\nBAD", format_hint=SupportedFormat.DXF)

    # Corrupt FlatGeobuf (truncated header)
    with pytest.raises(CorruptDataError):
        UnifiedIngestionDispatcher.ingest_bytes(b"fgb\x03fgb\x00\x00", format_hint=SupportedFormat.FLATGEOBUF)

    # Corrupt GeoTIFF (truncated header)
    with pytest.raises(CorruptDataError):
        UnifiedIngestionDispatcher.ingest_bytes(b"II*\x00\x00", format_hint=SupportedFormat.GEOTIFF)


# ---------------------------------------------------------------------------
# Celery Ingestion Pipeline Task Execution Tests
# ---------------------------------------------------------------------------


def test_celery_task_process_cadastral_layer_with_file_path(tmp_path: Path):
    """Verify Celery task processes layer file and returns structured ingestion metrics."""
    original_backend = celery_app.conf.result_backend
    celery_app.conf.result_backend = "cache+memory://"
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True

    try:
        sample_file = tmp_path / "layer.geojson"
        sample_file.write_bytes(_build_sample_geojson_bytes())

        layer_id = str(uuid.uuid4())
        task_res = process_cadastral_layer_task.delay(
            layer_id=layer_id,
            file_path=str(sample_file),
            target_crs="EPSG:4326",
        )

        assert task_res.successful()
        val = task_res.get()
        assert val["layer_id"] == layer_id
        assert val["format"] == "GEOJSON"
        assert val["detected_crs"] == "EPSG:4326"
        assert val["feature_count"] == 2
        assert val["is_raster"] is False
        assert val["status"] == "COMPLETED"
        assert val["action"] == "ingest_and_validate"
        assert len(val["bounds"]) == 4

    finally:
        celery_app.conf.result_backend = original_backend
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


def test_celery_task_process_cadastral_layer_backward_compatibility():
    """Verify process_cadastral_layer_task without file_path retains backwards compatibility."""
    original_backend = celery_app.conf.result_backend
    celery_app.conf.result_backend = "cache+memory://"
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True

    try:
        layer_id = str(uuid.uuid4())
        task_res = process_cadastral_layer_task.delay(layer_id)

        assert task_res.successful()
        val = task_res.get()
        assert val["layer_id"] == layer_id
        assert val["status"] == "PROCESSING"
        assert val["action"] == "ingest_and_validate"

    finally:
        celery_app.conf.result_backend = original_backend
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False
