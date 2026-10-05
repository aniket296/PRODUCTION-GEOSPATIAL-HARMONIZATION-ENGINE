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
    FormatDetectionResult,
    GeospatialFormatDetector,
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


# ---------------------------------------------------------------------------
# Detailed GeospatialFormatDetector and Ingestion Features Tests
# ---------------------------------------------------------------------------


def test_geospatial_format_detector_detailed_result():
    """Verify GeospatialFormatDetector returns complete FormatDetectionResult with confidence and MIME."""
    # FlatGeobuf magic bytes
    fgb_data = _build_sample_flatgeobuf_bytes()
    det_fgb = GeospatialFormatDetector.detect_bytes(fgb_data)
    assert isinstance(det_fgb, FormatDetectionResult)
    assert det_fgb.format == SupportedFormat.FLATGEOBUF
    assert det_fgb.confidence == 1.0
    assert det_fgb.mime_type == "application/vnd.flatgeobuf"
    assert det_fgb.detection_method == "MAGIC_BYTES"
    assert "magic" in det_fgb.details

    # GeoTIFF magic bytes
    gt_data = _build_sample_geotiff_bytes()
    det_gt = GeospatialFormatDetector.detect_bytes(gt_data)
    assert det_gt.format == SupportedFormat.GEOTIFF
    assert det_gt.confidence == 1.0
    assert det_gt.mime_type == "image/tiff"
    assert det_gt.detection_method == "MAGIC_BYTES"

    # Shapefile ZIP archive inspection
    shp_zip = _build_sample_shapefile_zip_bytes()
    det_shp = GeospatialFormatDetector.detect_bytes(shp_zip)
    assert det_shp.format == SupportedFormat.SHAPEFILE
    assert det_shp.confidence >= 0.95
    assert det_shp.mime_type == "application/zip"
    assert det_shp.detection_method == "ARCHIVE_INSPECTION"

    # GeoJSON text structure
    gj_data = _build_sample_geojson_bytes()
    det_gj = GeospatialFormatDetector.detect_bytes(gj_data)
    assert det_gj.format == SupportedFormat.GEOJSON
    assert det_gj.confidence >= 0.95
    assert det_gj.mime_type == "application/geo+json"
    assert det_gj.detection_method == "TEXT_STRUCTURE"

    # DXF ASCII text structure
    dxf_data = _build_sample_dxf_bytes()
    det_dxf = GeospatialFormatDetector.detect_bytes(dxf_data)
    assert det_dxf.format == SupportedFormat.DXF
    assert det_dxf.confidence >= 0.95
    assert det_dxf.mime_type == "image/vnd.dxf"
    assert det_dxf.detection_method == "TEXT_STRUCTURE"


def test_geospatial_format_detector_detect_file(tmp_path: Path):
    """Verify detect_file works for both files and directory-based shapefiles."""
    fgb_file = tmp_path / "test.fgb"
    fgb_file.write_bytes(_build_sample_flatgeobuf_bytes())
    res = GeospatialFormatDetector.detect_file(fgb_file)
    assert res.format == SupportedFormat.FLATGEOBUF
    assert res.confidence == 1.0

    # Directory with shapefile
    shp_dir = tmp_path / "cadastral_dir"
    shp_dir.mkdir()
    (shp_dir / "parcels.shp").write_bytes(struct.pack(">I", 9994) + b"\x00" * 96)
    (shp_dir / "parcels.shx").write_bytes(b"\x00" * 100)
    (shp_dir / "parcels.dbf").write_bytes(b"\x00" * 100)

    res_dir = GeospatialFormatDetector.detect_file(shp_dir)
    assert res_dir.format == SupportedFormat.SHAPEFILE
    assert res_dir.detection_method == "DIRECTORY_INSPECTION"


def test_unified_feature_record_to_dict_and_to_geojson():
    """Verify UnifiedFeatureRecord exports to standard dictionary and GeoJSON feature."""
    poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
    rec = UnifiedFeatureRecord(
        id="REC-001",
        geometry=poly,
        properties={"khasra_no": "42", "area_sqm": 100.0},
        crs="EPSG:4326",
        is_valid=True,
        bounds=(0.0, 0.0, 10.0, 10.0),
        area=100.0,
    )

    d = rec.to_dict()
    assert d["id"] == "REC-001"
    assert d["crs"] == "EPSG:4326"
    assert d["is_valid"] is True
    assert d["area"] == 100.0
    assert d["geometry_type"] == "Polygon"

    gj = rec.to_geojson_feature()
    assert gj["type"] == "Feature"
    assert gj["id"] == "REC-001"
    assert gj["geometry"]["type"] == "Polygon"
    assert gj["properties"]["khasra_no"] == "42"


def test_unified_ingestion_result_to_dict_and_to_geojson_collection():
    """Verify UnifiedIngestionResult exports summary dict and GeoJSON FeatureCollection."""
    data = _build_sample_geojson_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data)

    d = result.to_dict()
    assert d["format"] == "GEOJSON"
    assert d["detected_crs"] == "EPSG:4326"
    assert d["feature_count"] == 2
    assert d["is_raster"] is False
    assert "geometry_types" in d["metadata"]

    gj_fc = result.to_geojson_feature_collection()
    assert gj_fc["type"] == "FeatureCollection"
    assert len(gj_fc["features"]) == 2
    assert "crs" in gj_fc
    assert gj_fc["features"][0]["properties"]["khasra_no"] == "101/1"


def test_dispatcher_detect_convenience_methods(tmp_path: Path):
    """Verify UnifiedIngestionDispatcher convenience detection methods."""
    data = _build_sample_geojson_bytes()
    det = UnifiedIngestionDispatcher.detect(data)
    assert det.format == SupportedFormat.GEOJSON

    fpath = tmp_path / "cadastre.geojson"
    fpath.write_bytes(data)
    det_file = UnifiedIngestionDispatcher.detect_file(fpath)
    assert det_file.format == SupportedFormat.GEOJSON


def test_dispatcher_options_max_features_and_skip_invalid():
    """Verify max_features limit and skip_invalid filter options."""
    data = _build_sample_geojson_bytes()  # Contains 2 features
    res_limited = UnifiedIngestionDispatcher.ingest_bytes(data, max_features=1)
    assert res_limited.feature_count == 1
    assert len(res_limited.features) == 1

    res_all = UnifiedIngestionDispatcher.ingest_bytes(data, skip_invalid=True)
    assert res_all.feature_count == 2


def test_dispatcher_geometry_types_and_total_area_metadata():
    """Verify metadata calculates geometry type breakdown and total area."""
    data = _build_sample_geojson_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(data)

    assert "geometry_types" in result.metadata
    assert result.metadata["geometry_types"].get("Polygon") == 2
    assert "total_area_sqm" in result.metadata
    assert result.metadata["total_area_sqm"] > 0
    assert "detection_confidence" in result.metadata
    assert result.metadata["detection_confidence"] >= 0.90


def test_dispatcher_raster_cannot_export_geojson_feature_collection():
    """Verify raster datasets raise ValueError when exporting as GeoJSON FeatureCollection."""
    gt_data = _build_sample_geotiff_bytes()
    result = UnifiedIngestionDispatcher.ingest_bytes(gt_data)

    assert result.is_raster is True
    with pytest.raises(ValueError, match="Raster datasets cannot be exported"):
        result.to_geojson_feature_collection()

