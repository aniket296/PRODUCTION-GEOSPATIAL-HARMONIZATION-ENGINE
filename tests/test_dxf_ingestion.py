"""Tests for DXF binary/ASCII reader and cadastral entity extraction."""

from pathlib import Path
import pytest
from shapely.geometry import Point, Polygon

from backend.ingestion.dxf_reader import (
    DXFCorruptFileError,
    DXFEntityRecord,
    DXFIngestionReader,
    DXFIngestionResult,
)
from fixtures.defect_fixtures import generate_synthetic_dxf_cadastre, save_synthetic_dxf


def test_read_synthetic_dxf_from_text():
    dxf_text = generate_synthetic_dxf_cadastre()
    result = DXFIngestionReader.read_from_text(dxf_text)

    assert isinstance(result, DXFIngestionResult)
    # 10 parcels + 4 GCP points = 14 entities
    assert len(result.entities) == 14
    assert result.entity_counts.get("LWPOLYLINE") == 10
    assert result.entity_counts.get("POINT") == 4

    # Verify polygon geometries
    polygons = [e for e in result.entities if isinstance(e.geometry, Polygon)]
    assert len(polygons) == 10
    for poly in polygons:
        assert poly.layer == "PARCELS"
        assert poly.geometry.is_valid is True
        assert poly.geometry.area > 0

    # Verify point geometries
    points = [e for e in result.entities if isinstance(e.geometry, Point)]
    assert len(points) == 4
    for pt in points:
        assert pt.layer == "SURVEY_GCP"


def test_list_layers_from_dxf():
    dxf_text = generate_synthetic_dxf_cadastre()
    layers = DXFIngestionReader.list_layers(dxf_text)

    assert "PARCELS" in layers
    assert "SURVEY_GCP" in layers


def test_layer_filtering_parcels_only():
    dxf_text = generate_synthetic_dxf_cadastre()
    result = DXFIngestionReader.read_from_text(dxf_text, target_layers=["PARCELS"])

    assert len(result.entities) == 10
    assert result.entity_counts == {"LWPOLYLINE": 10}
    for e in result.entities:
        assert e.layer == "PARCELS"
        assert isinstance(e.geometry, Polygon)


def test_layer_filtering_gcp_only():
    dxf_text = generate_synthetic_dxf_cadastre()
    result = DXFIngestionReader.read_from_text(dxf_text, target_layers=["SURVEY_GCP"])

    assert len(result.entities) == 4
    assert result.entity_counts == {"POINT": 4}
    for e in result.entities:
        assert e.layer == "SURVEY_GCP"
        assert isinstance(e.geometry, Point)


def test_read_from_bytes_utf8_and_latin1():
    dxf_text = generate_synthetic_dxf_cadastre()
    utf8_bytes = dxf_text.encode("utf-8")
    result_utf8 = DXFIngestionReader.read_from_bytes(utf8_bytes)
    assert len(result_utf8.entities) == 14

    latin1_bytes = dxf_text.encode("latin-1")
    result_latin1 = DXFIngestionReader.read_from_bytes(latin1_bytes)
    assert len(result_latin1.entities) == 14


def test_read_from_file(tmp_path: Path):
    dxf_path = tmp_path / "cadastre.dxf"
    save_synthetic_dxf(dxf_path)

    result = DXFIngestionReader.read_from_file(dxf_path)
    assert len(result.entities) == 14
    assert result.bounds is not None
    minx, miny, maxx, maxy = result.bounds
    assert minx == 3600000.0
    assert miny == 500000.0
    assert maxx == 3600220.0
    assert maxy == 500100.0

    # Non-existent file raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        DXFIngestionReader.read_from_file(tmp_path / "non_existent.dxf")


def test_text_and_mtext_extraction():
    import ezdxf

    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    doc.layers.add("ANNOTATIONS")

    msp.add_text("KHASRA-101", dxfattribs={"layer": "ANNOTATIONS", "insert": (3600020.0, 500025.0)})
    msp.add_mtext("Owner: Rajesh Sharma", dxfattribs={"layer": "ANNOTATIONS", "insert": (3600020.0, 500030.0)})

    import io
    stream = io.StringIO()
    doc.write(stream)
    dxf_content = stream.getvalue()

    result = DXFIngestionReader.read_from_text(dxf_content, target_layers=["ANNOTATIONS"])
    assert len(result.entities) == 2

    texts = [e.properties.get("text") for e in result.entities]
    assert "KHASRA-101" in texts
    assert "Owner: Rajesh Sharma" in texts


def test_corrupt_and_empty_dxf_handling():
    with pytest.raises(DXFCorruptFileError, match="empty"):
        DXFIngestionReader.read_from_text("")

    with pytest.raises(DXFCorruptFileError, match="empty"):
        DXFIngestionReader.read_from_bytes(b"")

    with pytest.raises(DXFCorruptFileError):
        DXFIngestionReader.read_from_bytes(b"NOT_A_VALID_DXF_RANDOM_GARBAGE_PAYLOAD")
