"""Tests for Shapefile component validation (.shp, .shx, .dbf, .prj) and ZIP unpacking."""

import io
from pathlib import Path
import zipfile
import pytest
from shapely.geometry import Polygon
import shapefile  # pyshp

from backend.ingestion.shapefile_reader import (
    ShapefileComponentSet,
    ShapefileCorruptArchiveError,
    ShapefileIngestionReader,
    ShapefileIngestionResult,
    ShapefileMissingComponentError,
    ShapefileValidationError,
    parse_prj_to_crs,
    unpack_shapefile_zip,
    validate_shapefile_components,
)
from fixtures.cadastral_fixtures import generate_clean_cadastral_fixtures


# Reference WKT strings for testing
WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
)

EVEREST_1830_PRJ = (
    'PROJCS["Kalianpur 1975 / India Zone IIa",GEOGCS["Kalianpur 1975",'
    'DATUM["Kalianpur_1975",SPHEROID["Everest_1830_1975_Definition",6377299.151,300.8017]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Lambert_Conformal_Conic_2SP"],PARAMETER["False_Easting",3000000.0],'
    'PARAMETER["False_Northing",1000000.0],PARAMETER["Central_Meridian",74.0],'
    'PARAMETER["Standard_Parallel_1",29.0],PARAMETER["Standard_Parallel_2",35.0],'
    'PARAMETER["Latitude_Of_Origin",32.5],UNIT["Metre",1.0],AUTHORITY["EPSG","24378"]]'
)

UTM43N_PRJ = (
    'PROJCS["WGS_1984_UTM_Zone_43N",GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0.0],'
    'PARAMETER["central_meridian",75.0],PARAMETER["scale_factor",0.9996],'
    'PARAMETER["false_easting",500000.0],PARAMETER["false_northing",0.0],'
    'UNIT["Meter",1.0],AUTHORITY["EPSG","32643"]]'
)


def _create_sample_shapefile_on_disk(
    output_dir: Path,
    base_name: str = "cadastre_sample",
    prj_content: str = EVEREST_1830_PRJ,
) -> ShapefileComponentSet:
    """Helper to generate a complete valid Shapefile (.shp, .shx, .dbf, .prj) on disk."""
    output_dir.mkdir(parents=True, exist_ok=True)
    shp_base = str(output_dir / base_name)

    parcels = generate_clean_cadastral_fixtures()

    # Create Shapefile using pyshp
    with shapefile.Writer(shp_base, shapeType=shapefile.POLYGON) as w:
        w.field("khasra_no", "C", size=50)
        w.field("owner", "C", size=100)
        w.field("landuse", "C", size=50)
        w.field("area_sqm", "N", decimal=2)

        for p in parcels:
            poly: Polygon = p["geometry_everest"]
            coords = [list(pt) for pt in poly.exterior.coords]
            w.poly([coords])
            w.record(
                khasra_no=p["khasra_number"],
                owner=p["owner_name"],
                landuse=p["land_use"],
                area_sqm=p["area_sqm"],
            )

    # Write .prj
    prj_path = output_dir / f"{base_name}.prj"
    prj_path.write_text(prj_content, encoding="utf-8")

    all_files = [p for p in output_dir.glob("*") if p.is_file()]
    return validate_shapefile_components(all_files, base_name=base_name)


def test_parse_prj_to_crs():
    assert parse_prj_to_crs(WGS84_PRJ) == "EPSG:4326"
    assert parse_prj_to_crs(EVEREST_1830_PRJ) == "EPSG:24378"
    assert parse_prj_to_crs(UTM43N_PRJ) == "EPSG:32643"
    assert parse_prj_to_crs("") == "EPSG:4326"
    assert parse_prj_to_crs("UNKNOWN_PROJECTION_STRING") == "EPSG:4326"


def test_validate_shapefile_components_success(tmp_path: Path):
    comp_set = _create_sample_shapefile_on_disk(tmp_path / "valid_shp")
    assert comp_set.base_name == "cadastre_sample"
    assert comp_set.shp_path.exists()
    assert comp_set.shx_path.exists()
    assert comp_set.dbf_path.exists()
    assert comp_set.prj_path is not None and comp_set.prj_path.exists()

    d = comp_set.as_dict()
    assert "shp" in d and "shx" in d and "dbf" in d and "prj" in d


def test_validate_shapefile_missing_shp(tmp_path: Path):
    dir_path = tmp_path / "missing_shp"
    dir_path.mkdir(parents=True)
    (dir_path / "parcels.shx").write_bytes(b"dummy")
    (dir_path / "parcels.dbf").write_bytes(b"dummy")

    files = list(dir_path.glob("*"))
    with pytest.raises(ShapefileMissingComponentError, match="Missing mandatory geometry component: .shp"):
        validate_shapefile_components(files)


def test_validate_shapefile_missing_shx(tmp_path: Path):
    dir_path = tmp_path / "missing_shx"
    dir_path.mkdir(parents=True)
    (dir_path / "parcels.shp").write_bytes(b"dummy")
    (dir_path / "parcels.dbf").write_bytes(b"dummy")

    files = list(dir_path.glob("*"))
    with pytest.raises(ShapefileMissingComponentError, match="Missing mandatory index component: parcels.shx"):
        validate_shapefile_components(files)


def test_validate_shapefile_missing_dbf(tmp_path: Path):
    dir_path = tmp_path / "missing_dbf"
    dir_path.mkdir(parents=True)
    (dir_path / "parcels.shp").write_bytes(b"dummy")
    (dir_path / "parcels.shx").write_bytes(b"dummy")

    files = list(dir_path.glob("*"))
    with pytest.raises(ShapefileMissingComponentError, match="Missing mandatory attribute table component: parcels.dbf"):
        validate_shapefile_components(files)


def test_validate_multiple_shapefiles_without_base_name(tmp_path: Path):
    dir_path = tmp_path / "multiple_shp"
    dir_path.mkdir(parents=True)
    for stem in ("layer1", "layer2"):
        (dir_path / f"{stem}.shp").write_bytes(b"dummy")
        (dir_path / f"{stem}.shx").write_bytes(b"dummy")
        (dir_path / f"{stem}.dbf").write_bytes(b"dummy")

    files = list(dir_path.glob("*"))
    with pytest.raises(ShapefileValidationError, match="Multiple Shapefile sets detected"):
        validate_shapefile_components(files)

    # Specifying base_name resolves ambiguity
    comp1 = validate_shapefile_components(files, base_name="layer1")
    assert comp1.base_name == "layer1"


def test_unpack_shapefile_zip(tmp_path: Path):
    shp_dir = tmp_path / "raw_shp"
    _create_sample_shapefile_on_disk(shp_dir, base_name="village_cadastre")

    zip_path = tmp_path / "village_cadastre.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in shp_dir.glob("village_cadastre.*"):
            zf.write(f, arcname=f.name)

    extract_dir = tmp_path / "unpacked"
    comp_set = unpack_shapefile_zip(zip_path, extract_dir=extract_dir)
    assert comp_set.base_name == "village_cadastre"
    assert comp_set.shp_path.exists()
    assert comp_set.shx_path.exists()
    assert comp_set.dbf_path.exists()


def test_unpack_corrupt_zip(tmp_path: Path):
    corrupt_zip = tmp_path / "corrupt.zip"
    corrupt_zip.write_bytes(b"THIS_IS_NOT_A_VALID_ZIP_HEADER")

    with pytest.raises(ShapefileCorruptArchiveError, match="Invalid or corrupted ZIP archive"):
        unpack_shapefile_zip(corrupt_zip, extract_dir=tmp_path / "out")


def test_read_shapefile_from_directory(tmp_path: Path):
    shp_dir = tmp_path / "full_dir"
    _create_sample_shapefile_on_disk(shp_dir, base_name="parcels", prj_content=EVEREST_1830_PRJ)

    result = ShapefileIngestionReader.read_from_directory(shp_dir)
    assert isinstance(result, ShapefileIngestionResult)
    assert result.total_count == 10
    assert result.valid_count == 10
    assert result.detected_crs == "EPSG:24378"

    for feat in result.features:
        assert isinstance(feat.geometry, Polygon)
        assert feat.geometry.exterior.is_ccw is True
        assert feat.is_valid is True
        assert feat.area > 0
        assert "khasra_number" in feat.properties
        assert "owner_name" in feat.properties
        assert "land_use" in feat.properties

    assert result.bounds is not None
    minx, miny, maxx, maxy = result.bounds
    assert minx == 3600000.0
    assert miny == 500000.0


def test_read_shapefile_from_zip(tmp_path: Path):
    shp_dir = tmp_path / "zip_source"
    _create_sample_shapefile_on_disk(shp_dir, base_name="parcels", prj_content=UTM43N_PRJ)

    zip_bytes_io = io.BytesIO()
    with zipfile.ZipFile(zip_bytes_io, "w") as zf:
        for f in shp_dir.glob("parcels.*"):
            zf.write(f, arcname=f.name)

    zip_bytes = zip_bytes_io.getvalue()
    result = ShapefileIngestionReader.read_from_zip(zip_bytes)

    assert result.total_count == 10
    assert result.detected_crs == "EPSG:32643"
    assert result.valid_count == 10
    assert result.invalid_count == 0
