"""Tests for DXF binary/ASCII reader and cadastral entity extraction."""

from io import StringIO
from pathlib import Path
import pytest
from shapely.geometry import LineString, Point, Polygon

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
    assert result.layer_counts.get("PARCELS") == 10
    assert result.layer_counts.get("SURVEY_GCP") == 4

    # Verify polygon geometries
    polygons = [e for e in result.entities if isinstance(e.geometry, Polygon)]
    assert len(polygons) == 10
    for poly in polygons:
        assert poly.layer == "PARCELS"
        assert poly.geometry.is_valid is True
        assert poly.geometry.area > 0
        assert poly.properties["is_closed"] is True
        assert poly.properties["vertex_count"] == 4
        assert poly.properties["area_sqm"] > 0

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


def test_layer_regex_pattern_filtering():
    dxf_text = generate_synthetic_dxf_cadastre()
    result = DXFIngestionReader.read_from_text(dxf_text, layer_pattern=r"^PARCEL.*")

    assert len(result.entities) == 10
    for e in result.entities:
        assert e.layer == "PARCELS"

    result_gcp = DXFIngestionReader.read_from_text(dxf_text, layer_pattern=r".*GCP$")
    assert len(result_gcp.entities) == 4
    for e in result_gcp.entities:
        assert e.layer == "SURVEY_GCP"


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

    stream = StringIO()
    doc.write(stream)
    dxf_content = stream.getvalue()

    result = DXFIngestionReader.read_from_text(dxf_content, target_layers=["ANNOTATIONS"])
    assert len(result.entities) == 2

    texts = [e.properties.get("text") for e in result.entities]
    assert "KHASRA-101" in texts
    assert "Owner: Rajesh Sharma" in texts


def test_spatial_linking_of_khasra_labels_to_polygons():
    import ezdxf

    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    doc.layers.add("PARCELS")
    doc.layers.add("LABELS")

    # Add a closed parcel polygon (0,0) to (50, 50)
    msp.add_lwpolyline([(0, 0), (50, 0), (50, 50), (0, 50)], close=True, dxfattribs={"layer": "PARCELS"})
    # Add an interior text label (25, 25)
    msp.add_text("KHASRA 204", dxfattribs={"layer": "LABELS", "insert": (25, 25)})

    stream = StringIO()
    doc.write(stream)
    dxf_content = stream.getvalue()

    result = DXFIngestionReader.read_from_text(dxf_content, link_annotations=True)
    assert len(result.entities) == 2

    poly_rec = [e for e in result.entities if isinstance(e.geometry, Polygon)][0]
    assert "enclosed_labels" in poly_rec.properties
    assert poly_rec.properties["enclosed_labels"] == ["KHASRA 204"]
    assert poly_rec.properties["khasra_number"] == "204"


def test_line_and_insert_entity_extraction():
    import ezdxf

    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    doc.layers.add("SURVEY_TIES")
    doc.layers.add("CONTROL_PINS")

    msp.add_line((10.0, 10.0), (20.0, 20.0), dxfattribs={"layer": "SURVEY_TIES"})
    msp.add_blockref("BENCHMARK_PIN", insert=(15.0, 15.0), dxfattribs={"layer": "CONTROL_PINS"})

    stream = StringIO()
    doc.write(stream)
    dxf_content = stream.getvalue()

    result = DXFIngestionReader.read_from_text(dxf_content)
    assert len(result.entities) == 2
    assert result.entity_counts.get("LINE") == 1
    assert result.entity_counts.get("INSERT") == 1

    line_rec = [e for e in result.entities if e.entity_type == "LINE"][0]
    assert isinstance(line_rec.geometry, LineString)
    assert line_rec.properties["length_m"] > 0

    insert_rec = [e for e in result.entities if e.entity_type == "INSERT"][0]
    assert isinstance(insert_rec.geometry, Point)
    assert insert_rec.properties["block_name"] == "BENCHMARK_PIN"


def test_self_intersecting_polyline_stored_with_warning():
    import ezdxf

    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    doc.layers.add("INVALID_LAYER")

    # Bow-tie self-intersecting polygon
    msp.add_lwpolyline([(0, 0), (50, 50), (50, 0), (0, 50)], close=True, dxfattribs={"layer": "INVALID_LAYER"})

    stream = StringIO()
    doc.write(stream)
    dxf_content = stream.getvalue()

    result = DXFIngestionReader.read_from_text(dxf_content)
    assert len(result.entities) == 1
    assert result.invalid_geometries_count == 1

    rec = result.entities[0]
    # Stored as LineString rather than invalid Polygon
    assert isinstance(rec.geometry, LineString)
    assert "topology_warning" in rec.properties


def test_corrupt_and_empty_dxf_handling():
    with pytest.raises(DXFCorruptFileError, match="empty"):
        DXFIngestionReader.read_from_text("")

    with pytest.raises(DXFCorruptFileError, match="empty"):
        DXFIngestionReader.read_from_bytes(b"")

    with pytest.raises(DXFCorruptFileError):
        DXFIngestionReader.read_from_bytes(b"NOT_A_VALID_DXF_RANDOM_GARBAGE_PAYLOAD")
