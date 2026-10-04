"""Tests for multi-page TIFF tag extraction and GeoTIFF ModelTiepointTag parsing."""

from pathlib import Path
import struct

import pytest

from backend.ingestion.geotiff_reader import (
    GEOKEY_GEOG_SEMI_MAJOR_AXIS,
    GEOKEY_GT_CITATION,
    MODEL_TYPE_GEOGRAPHIC,
    RASTER_PIXEL_IS_POINT,
    TAG_GEO_KEY_DIRECTORY,
    TAG_IMAGE_WIDTH,
    TAG_MODEL_TIEPOINT,
    TAG_X_RESOLUTION,
    TIFF_TYPE_DOUBLE,
    TIFF_TYPE_LONG,
    TIFF_TYPE_LONG8,
    TIFF_TYPE_RATIONAL,
    TIFF_TYPE_SHORT,
    GeoTIFFCorruptIFDError,
    GeoTIFFGeoreferenceError,
    GeoTIFFIngestionReader,
    GeoTIFFIngestionResult,
    GeoTIFFInvalidHeaderError,
    GeoTIFFMissingGeoreferenceError,
    ModelTiepoint,
    RasterMetadata,
    SyntheticGeoTIFFPage,
    build_synthetic_geotiff,
    compute_geotransform,
    fit_affine_from_tiepoints,
    model_to_pixel,
    parse_geokey_directory,
    parse_model_pixel_scale,
    parse_model_tiepoints,
    parse_model_transformation,
    parse_tiff_header,
    pixel_to_model,
    raster_footprint,
    read_tiff_pages,
    read_tiff_pages_from_file,
)


# --- Header parsing --------------------------------------------------------


@pytest.mark.parametrize("byte_order,mark", [("<", b"II"), (">", b"MM")])
def test_parse_classic_header_both_byte_orders(byte_order, mark):
    payload = build_synthetic_geotiff(byte_order=byte_order)
    assert payload[:2] == mark
    header = parse_tiff_header(payload)
    assert header.byte_order == byte_order
    assert header.is_bigtiff is False
    assert header.header_size == 8
    assert header.first_ifd_offset == 8


def test_parse_bigtiff_header():
    payload = build_synthetic_geotiff(bigtiff=True)
    header = parse_tiff_header(payload)
    assert header.is_bigtiff is True
    assert header.header_size == 16
    assert header.first_ifd_offset == 16


def test_invalid_header_rejections():
    with pytest.raises(GeoTIFFInvalidHeaderError):
        parse_tiff_header(b"")
    with pytest.raises(GeoTIFFInvalidHeaderError):
        parse_tiff_header(b"II*\x00")
    with pytest.raises(GeoTIFFInvalidHeaderError, match="byte-order"):
        parse_tiff_header(b"XX*\x00\x08\x00\x00\x00")
    with pytest.raises(GeoTIFFInvalidHeaderError, match="version"):
        parse_tiff_header(b"II\x2b\x01\x08\x00\x00\x00" + b"\x00" * 16)
    # BigTIFF with invalid offset byte size
    with pytest.raises(GeoTIFFInvalidHeaderError, match="BigTIFF"):
        parse_tiff_header(b"II\x2b\x00\x04\x00\x00\x00" + struct.pack("<Q", 16) + b"\x00" * 8)


def test_first_ifd_offset_out_of_bounds():
    with pytest.raises(GeoTIFFCorruptIFDError):
        parse_tiff_header(b"II*\x00" + struct.pack("<I", 4096) + b"\x00" * 8)


# --- Multi-page IFD chain ---------------------------------------------------


def test_multi_page_extraction_with_overviews():
    pages_spec = [
        SyntheticGeoTIFFPage(width=8, height=6, description="Ward 12 cadastral sheet"),
        SyntheticGeoTIFFPage(width=4, height=3, new_subfile_type=1, pixel_scale=(1.0, 1.0, 0.0)),
        SyntheticGeoTIFFPage(width=2, height=2, new_subfile_type=1, pixel_scale=(2.0, 2.0, 0.0)),
    ]
    pages = read_tiff_pages(build_synthetic_geotiff(pages_spec))

    assert len(pages) == 3
    assert [p.index for p in pages] == [0, 1, 2]
    assert [(p.width, p.height) for p in pages] == [(8, 6), (4, 3), (2, 2)]
    assert pages[0].is_reduced_resolution is False
    assert pages[1].is_reduced_resolution is True
    assert pages[2].is_reduced_resolution is True
    assert pages[0].description == "Ward 12 cadastral sheet"
    assert pages[2].model_pixel_scale == (2.0, 2.0, 0.0)
    assert len({p.offset for p in pages}) == 3


@pytest.mark.parametrize("byte_order", ["<", ">"])
@pytest.mark.parametrize("bigtiff", [False, True])
def test_multi_page_roundtrip_all_layouts(byte_order, bigtiff):
    spec = [SyntheticGeoTIFFPage(width=5, height=4), SyntheticGeoTIFFPage(width=3, height=2, new_subfile_type=1)]
    pages = read_tiff_pages(build_synthetic_geotiff(spec, byte_order=byte_order, bigtiff=bigtiff))
    assert [(p.width, p.height) for p in pages] == [(5, 4), (3, 2)]
    assert pages[0].geokey_directory.epsg == 24378


def test_tag_types_inline_and_offset_values():
    payload = build_synthetic_geotiff([SyntheticGeoTIFFPage(width=6, height=5, samples_per_pixel=3)])
    page = read_tiff_pages(payload)[0]

    assert page.tags[TAG_IMAGE_WIDTH].field_type == TIFF_TYPE_LONG
    assert page.tags[TAG_IMAGE_WIDTH].scalar == 6
    assert page.samples_per_pixel == 3
    assert page.bits_per_sample == (8, 8, 8)  # 3 SHORTs = 6 bytes, stored out-of-line
    assert page.tags[TAG_X_RESOLUTION].field_type == TIFF_TYPE_RATIONAL
    assert page.tags[TAG_X_RESOLUTION].scalar == pytest.approx(72.0)
    assert page.tags[TAG_MODEL_TIEPOINT].field_type == TIFF_TYPE_DOUBLE
    assert page.tags[TAG_GEO_KEY_DIRECTORY].field_type == TIFF_TYPE_SHORT
    assert page.compression == 1
    assert page.photometric == 2


def test_bigtiff_uses_long8_strip_offsets_and_valid_strip_data():
    payload = build_synthetic_geotiff([SyntheticGeoTIFFPage(width=4, height=3)], bigtiff=True)
    page = read_tiff_pages(payload)[0]
    strip_tag = page.tags[273]
    assert strip_tag.field_type == TIFF_TYPE_LONG8
    offset, size = strip_tag.scalar, page.tags[279].scalar
    assert size == 12
    assert payload[offset : offset + size] == bytes((n * 7) % 256 for n in range(12))


def test_ifd_loop_detection():
    payload = bytearray(build_synthetic_geotiff())
    page = read_tiff_pages(bytes(payload))[0]
    num_entries = struct.unpack("<H", payload[8:10])[0]
    next_ptr_pos = 8 + 2 + num_entries * 12
    payload[next_ptr_pos : next_ptr_pos + 4] = struct.pack("<I", page.offset)  # point to itself
    with pytest.raises(GeoTIFFCorruptIFDError, match="loop"):
        read_tiff_pages(bytes(payload))


def test_truncated_ifd_and_out_of_bounds_tag_value():
    payload = build_synthetic_geotiff()
    with pytest.raises(GeoTIFFCorruptIFDError):
        read_tiff_pages(payload[:40])

    corrupt = bytearray(payload)
    num_entries = struct.unpack("<H", corrupt[8:10])[0]
    for n in range(num_entries):
        pos = 10 + n * 12
        if struct.unpack("<H", corrupt[pos : pos + 2])[0] == TAG_MODEL_TIEPOINT:
            corrupt[pos + 8 : pos + 12] = struct.pack("<I", 10_000_000)
    with pytest.raises(GeoTIFFCorruptIFDError, match="exceeds payload"):
        read_tiff_pages(bytes(corrupt))


def test_zero_entry_ifd_rejected():
    payload = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 0) + struct.pack("<I", 0)
    with pytest.raises(GeoTIFFCorruptIFDError, match="zero entries"):
        read_tiff_pages(payload)


# --- ModelTiepointTag / scale / transformation ------------------------------


def test_model_tiepoint_single_with_pixel_scale():
    page = read_tiff_pages(build_synthetic_geotiff())[0]
    assert page.model_tiepoints == [ModelTiepoint(0.0, 0.0, 0.0, 3600000.0, 500100.0, 0.0)]
    assert page.model_pixel_scale == (0.5, 0.5, 0.0)
    assert page.model_transformation is None
    assert page.has_georeference is True


def test_model_tiepoint_multiple_gcps_without_scale():
    gcps = [
        (0.0, 0.0, 0.0, 76.7790, 30.7333, 0.0),
        (99.0, 0.0, 0.0, 76.7800, 30.7333, 0.0),
        (99.0, 99.0, 0.0, 76.7800, 30.7323, 0.0),
        (0.0, 99.0, 0.0, 76.7790, 30.7323, 0.0),
    ]
    spec = SyntheticGeoTIFFPage(
        width=100, height=100, tiepoints=gcps, pixel_scale=None, epsg=4326, model_type=MODEL_TYPE_GEOGRAPHIC
    )
    page = read_tiff_pages(build_synthetic_geotiff([spec]))[0]
    tps = page.model_tiepoints
    assert len(tps) == 4
    assert (tps[2].i, tps[2].j) == (99.0, 99.0)
    assert (tps[2].x, tps[2].y) == pytest.approx((76.7800, 30.7323))
    assert page.model_pixel_scale is None
    assert page.has_georeference is True


def test_model_transformation_tag():
    matrix = (0.5, 0.0, 0.0, 700000.0, 0.0, -0.5, 0.0, 3105000.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    spec = SyntheticGeoTIFFPage(tiepoints=None, pixel_scale=None, transformation=matrix, epsg=32643)
    page = read_tiff_pages(build_synthetic_geotiff([spec]))[0]
    assert page.model_transformation == matrix
    assert page.model_tiepoints == []
    assert page.has_georeference is True


def test_non_georeferenced_page():
    spec = SyntheticGeoTIFFPage(tiepoints=None, pixel_scale=None, epsg=None)
    page = read_tiff_pages(build_synthetic_geotiff([spec]))[0]
    assert page.has_georeference is False
    assert page.geokey_directory is None


def test_georeference_value_validation():
    with pytest.raises(GeoTIFFGeoreferenceError):
        parse_model_tiepoints([0.0, 0.0, 0.0, 1.0, 2.0])
    with pytest.raises(GeoTIFFGeoreferenceError):
        parse_model_tiepoints([])
    with pytest.raises(GeoTIFFGeoreferenceError):
        parse_model_pixel_scale([1.0, 1.0])
    with pytest.raises(GeoTIFFGeoreferenceError, match="degenerate"):
        parse_model_pixel_scale([0.0, 1.0, 0.0])
    with pytest.raises(GeoTIFFGeoreferenceError):
        parse_model_transformation([1.0] * 12)


# --- GeoKeyDirectory / CRS ---------------------------------------------------


@pytest.mark.parametrize(
    "epsg,model_type,expected",
    [
        (24378, 1, "EPSG:24378"),
        (32643, 1, "EPSG:32643"),
        (4326, MODEL_TYPE_GEOGRAPHIC, "EPSG:4326"),
    ],
)
def test_geokey_directory_crs_identification(epsg, model_type, expected):
    spec = SyntheticGeoTIFFPage(epsg=epsg, model_type=model_type)
    gk = read_tiff_pages(build_synthetic_geotiff([spec]))[0].geokey_directory
    assert gk.version == 1
    assert gk.model_type == model_type
    assert gk.epsg == epsg
    assert gk.canonical_crs == expected


def test_geokey_ascii_and_double_params_resolution():
    spec = SyntheticGeoTIFFPage(
        epsg=24378,
        raster_type=RASTER_PIXEL_IS_POINT,
        citation="Kalianpur 1975 / India zone IIa",
        geo_double_keys={GEOKEY_GEOG_SEMI_MAJOR_AXIS: 6377299.36559538},
    )
    gk = read_tiff_pages(build_synthetic_geotiff([spec], byte_order=">"))[0].geokey_directory
    assert gk.citation == "Kalianpur 1975 / India zone IIa"
    assert gk.keys[GEOKEY_GT_CITATION] == "Kalianpur 1975 / India zone IIa"
    assert gk.keys[GEOKEY_GEOG_SEMI_MAJOR_AXIS] == pytest.approx(6377299.36559538)
    assert gk.is_pixel_is_point is True
    assert gk.named_keys()["ProjectedCSTypeGeoKey"] == 24378


def test_geokey_user_defined_and_malformed_directories():
    user_defined = parse_geokey_directory([1, 1, 0, 2, 1024, 0, 1, 1, 3072, 0, 1, 32767])
    assert user_defined.epsg is None
    assert user_defined.canonical_crs is None

    with pytest.raises(GeoTIFFGeoreferenceError):
        parse_geokey_directory([1, 1, 0])
    with pytest.raises(GeoTIFFGeoreferenceError, match="version"):
        parse_geokey_directory([2, 1, 0, 0])
    with pytest.raises(GeoTIFFGeoreferenceError, match="declares"):
        parse_geokey_directory([1, 1, 0, 2, 1024, 0, 1, 1])
    with pytest.raises(GeoTIFFGeoreferenceError, match="GeoDoubleParams"):
        parse_geokey_directory([1, 1, 0, 1, 2057, 34736, 1, 0], double_params=())
    with pytest.raises(GeoTIFFGeoreferenceError, match="location"):
        parse_geokey_directory([1, 1, 0, 1, 2057, 9999, 1, 0])


# --- File access --------------------------------------------------------------


def test_read_tiff_pages_from_file(tmp_path: Path):
    tif = tmp_path / "ortho_ward12.tif"
    tif.write_bytes(build_synthetic_geotiff([SyntheticGeoTIFFPage(), SyntheticGeoTIFFPage(new_subfile_type=1)]))
    pages = read_tiff_pages_from_file(tif)
    assert len(pages) == 2
    assert pages[0].geokey_directory.canonical_crs == "EPSG:24378"

    with pytest.raises(FileNotFoundError):
        read_tiff_pages_from_file(tmp_path / "missing.tif")


def test_synthetic_builder_is_deterministic():
    spec = [SyntheticGeoTIFFPage(citation="Everest 1830"), SyntheticGeoTIFFPage(new_subfile_type=1)]
    assert build_synthetic_geotiff(spec) == build_synthetic_geotiff(spec)


# --- GeoTIFFIngestionReader & Spatial Extent ---------------------------------


def test_geotiff_ingestion_reader_tiepoint_and_scale(tmp_path: Path):
    """Verify end-to-end reader extracts correct dimensions, bounds, footprint, and metadata."""
    spec = SyntheticGeoTIFFPage(
        width=100,
        height=80,
        tiepoints=[(0.0, 0.0, 0.0, 3600000.0, 500000.0, 0.0)],
        pixel_scale=(0.5, 0.5, 0.0),
        epsg=24378,
        description="Rohtak cadastre map",
        nodata="-9999",
    )
    payload = build_synthetic_geotiff([spec])
    tif_file = tmp_path / "rohtak_sheet.tif"
    tif_file.write_bytes(payload)

    # 1. Read from file
    res = GeoTIFFIngestionReader.read_from_file(tif_file)
    assert isinstance(res, GeoTIFFIngestionResult)
    assert res.is_valid is True
    meta = res.metadata
    assert meta.width == 100
    assert meta.height == 80
    assert meta.band_count == 1
    assert meta.data_type == "uint8"
    assert meta.compression == "none"
    assert meta.nodata == -9999.0
    assert meta.crs == "EPSG:24378"
    assert meta.pixel_size == (0.5, 0.5)
    assert meta.is_rotated is False

    # Bounds: X from 3600000.0 to 3600000 + 100*0.5 = 3600050.0
    # Y from 500000 - 80*0.5 = 499960.0 to 500000.0
    assert meta.bounds == (3600000.0, 499960.0, 3600050.0, 500000.0)
    assert meta.footprint is not None
    assert meta.footprint.is_valid is True
    assert meta.footprint.exterior.is_ccw is True
    assert meta.footprint_area == pytest.approx(50.0 * 40.0)

    # 2. Read from bytes matches
    res_bytes = GeoTIFFIngestionReader.read_from_bytes(payload)
    assert res_bytes.metadata.bounds == meta.bounds
    assert res_bytes.detected_crs == "EPSG:24378"


def test_geotiff_ingestion_reader_transformation_matrix(tmp_path: Path):
    """Verify reader supports 4x4 row-major affine transformation matrices with rotation."""
    # Affine matrix: 30-degree rotation, scale 1.0, translation (700000, 3100000)
    # X = 700000 + 0.866025*col - 0.5*row
    # Y = 3100000 + 0.5*col + 0.866025*row
    matrix = (
        0.8660254037844386, -0.5, 0.0, 700000.0,
        0.5, 0.8660254037844386, 0.0, 3100000.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    spec = SyntheticGeoTIFFPage(
        width=50,
        height=50,
        tiepoints=None,
        pixel_scale=None,
        transformation=matrix,
        epsg=32643,
    )
    payload = build_synthetic_geotiff([spec])
    res = GeoTIFFIngestionReader.read_from_bytes(payload)

    assert res.is_valid is True
    assert res.metadata.is_rotated is True
    gt = res.metadata.geotransform
    assert gt is not None

    # Test coordinate inversion roundtrip
    test_cols_rows = [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (25.5, 12.3)]
    for col, row in test_cols_rows:
        x, y = pixel_to_model(gt, col, row)
        col_inv, row_inv = model_to_pixel(gt, x, y)
        assert col_inv == pytest.approx(col, abs=1e-6)
        assert row_inv == pytest.approx(row, abs=1e-6)


def test_geotiff_ingestion_reader_gcp_least_squares_fit():
    """Verify reader fits affine geotransform from >= 3 GCPs with residual calculation."""
    gcps = [
        (0.0, 0.0, 0.0, 100.0, 200.0, 0.0),
        (10.0, 0.0, 0.0, 120.0, 200.0, 0.0),
        (10.0, 10.0, 0.0, 120.0, 180.0, 0.0),
        (0.0, 10.0, 0.0, 100.0, 180.0, 0.0),
    ]
    spec = SyntheticGeoTIFFPage(
        width=10,
        height=10,
        tiepoints=gcps,
        pixel_scale=None,
        epsg=24378,
    )
    payload = build_synthetic_geotiff([spec])
    res = GeoTIFFIngestionReader.read_from_bytes(payload)

    assert res.is_valid is True
    assert res.metadata.gcp_fit is not None
    assert res.metadata.gcp_fit.gcp_count == 4
    assert res.metadata.gcp_fit.rms_error == pytest.approx(0.0, abs=1e-6)
    assert any("GCPs" in w for w in res.warnings)


def test_geotiff_pixel_is_point_shift():
    """Verify PixelIsPoint rasters apply half-pixel shift to geotransform origin."""
    spec_area = SyntheticGeoTIFFPage(
        width=10, height=10,
        tiepoints=[(0.0, 0.0, 0.0, 100.0, 100.0, 0.0)],
        pixel_scale=(2.0, 2.0, 0.0),
        raster_type=1,  # PixelIsArea
    )
    spec_point = SyntheticGeoTIFFPage(
        width=10, height=10,
        tiepoints=[(0.0, 0.0, 0.0, 100.0, 100.0, 0.0)],
        pixel_scale=(2.0, 2.0, 0.0),
        raster_type=2,  # PixelIsPoint
    )

    res_area = GeoTIFFIngestionReader.read_from_bytes(build_synthetic_geotiff([spec_area]))
    res_point = GeoTIFFIngestionReader.read_from_bytes(build_synthetic_geotiff([spec_point]))

    gt_area = res_area.metadata.geotransform
    gt_point = res_point.metadata.geotransform

    # In PixelIsPoint, origin is shifted by half pixel (-0.5*scale_x, +0.5*scale_y)
    assert gt_point[0] == pytest.approx(gt_area[0] - 1.0)
    assert gt_point[3] == pytest.approx(gt_area[3] + 1.0)


def test_geotiff_missing_georeference():
    """Verify behavior when raster lacks georeferencing tags."""
    spec = SyntheticGeoTIFFPage(tiepoints=None, pixel_scale=None, epsg=None)
    payload = build_synthetic_geotiff([spec])

    with pytest.raises(GeoTIFFMissingGeoreferenceError):
        GeoTIFFIngestionReader.read_from_bytes(payload, require_georeference=True)

    res = GeoTIFFIngestionReader.read_from_bytes(payload, require_georeference=False)
    assert res.metadata.geotransform is None
    assert res.metadata.bounds is None
    assert res.is_valid is False
    assert any("not georeferenced" in w for w in res.warnings)


def test_geotiff_collinear_gcps_raises_error():
    """Verify collinear GCPs produce an error during least-squares fit."""
    collinear = [
        ModelTiepoint(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        ModelTiepoint(1.0, 1.0, 0.0, 10.0, 10.0, 0.0),
        ModelTiepoint(2.0, 2.0, 0.0, 20.0, 20.0, 0.0),
    ]
    with pytest.raises(GeoTIFFGeoreferenceError, match="collinear"):
        fit_affine_from_tiepoints(collinear)


def test_geotiff_singular_geotransform_raises_error():
    """Verify singular geotransform determinant raises error on model_to_pixel inversion."""
    singular_gt = (100.0, 1.0, 2.0, 200.0, 2.0, 4.0)  # det = 1*4 - 2*2 = 0
    with pytest.raises(GeoTIFFGeoreferenceError, match="singular"):
        model_to_pixel(singular_gt, 150.0, 250.0)


def test_geotiff_geographic_out_of_bounds_validation():
    """Verify rasters with out-of-range geographic coordinates fail validation."""
    spec = SyntheticGeoTIFFPage(
        width=10,
        height=10,
        tiepoints=[(0.0, 0.0, 0.0, 250.0, 45.0, 0.0)],  # Lon 250 > 180
        pixel_scale=(1.0, 1.0, 0.0),
        epsg=4326,
        model_type=MODEL_TYPE_GEOGRAPHIC,
    )
    res = GeoTIFFIngestionReader.read_from_bytes(build_synthetic_geotiff([spec]))
    assert res.is_valid is False
    assert any("limits" in w for w in res.warnings)


def test_geotiff_overviews_and_metadata_dict():
    """Verify overviews extraction and to_dict serialization."""
    spec_main = SyntheticGeoTIFFPage(width=16, height=16, description="Aerial image")
    spec_ovr1 = SyntheticGeoTIFFPage(width=8, height=8, new_subfile_type=1)
    spec_ovr2 = SyntheticGeoTIFFPage(width=4, height=4, new_subfile_type=1)

    res = GeoTIFFIngestionReader.read_from_bytes(build_synthetic_geotiff([spec_main, spec_ovr1, spec_ovr2]))
    meta = res.metadata
    assert meta.overview_count == 2
    assert meta.overview_sizes == [(8, 8), (4, 4)]
    assert meta.page_count == 3

    d = meta.to_dict()
    assert d["width"] == 16
    assert d["height"] == 16
    assert d["overview_count"] == 2
    assert d["crs"] == "EPSG:24378"
    assert "footprint_wkt" in d

