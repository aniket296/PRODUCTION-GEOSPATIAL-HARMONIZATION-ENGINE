"""Comprehensive test suite for cadastre boundary normalization and topology preservation.

Validates:
1. Geometry sanitization: unclosed rings, duplicate vertices, collinear decimation, spikes, bow-ties.
2. Ring orientation: OGC Simple Features / RFC 7946 (exterior CCW, interior CW).
3. Coordinate snapping and precision grid alignment.
4. Legal area preservation bounds and shape fidelity (Hausdorff distance).
5. Topological edge snapping between adjacent cadastral parcels.
6. Planar graph validation and anomaly diagnostics (micro-overlaps, disputes, slivers, gaps, dangling nodes).
7. Integration with authentic Indian cadastral fixtures (Everest 1830 / Kalianpur 1975 grid).
"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, List
import pytest
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from backend.geodetic.normalization import (
    CadastralNormalizationResult,
    CadastralTopologyPreserver,
    CadastralTopologyReport,
    CadastralTopologyValidator,
    DefectSeverity,
    DefectType,
    GeometrySanitizer,
    NormalizationAudit,
    NormalizationConfig,
    NormalizationError,
    TopologicalAnomaly,
    calculate_polsby_popper_compactness,
)
from fixtures.cadastral_fixtures import (
    EVEREST_1830_CRS,
    generate_clean_cadastral_fixtures,
)
from fixtures.defect_fixtures import (
    DEFECT_BOUNDARY_GAP,
    DEFECT_CRITICAL_OVERLAP,
    DEFECT_MICRO_OVERLAP,
    DEFECT_SELF_INTERSECTION,
    DEFECT_SLIVER_POLYGON,
    create_self_intersecting_polygon,
    create_sliver_polygon,
    generate_defective_cadastral_dataset,
    inject_boundary_gap,
    inject_critical_dispute_overlap,
    inject_micro_overlap,
)


# =====================================================================
# 1. GeometrySanitizer Unit Tests
# =====================================================================

class TestGeometrySanitizer:
    """Tests for low-level boundary sanitization and ring repair algorithms."""

    def test_unclosed_ring_automatic_closure(self):
        # 4 distinct points forming a rectangle, but not closed (point 0 != point -1)
        unclosed_coords = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        closed, was_mod = GeometrySanitizer.ensure_ring_closed(unclosed_coords)

        assert was_mod is True
        assert len(closed) == 5
        assert closed[0] == closed[-1]
        assert closed[-1] == (0.0, 0.0)

    def test_already_closed_ring_unchanged(self):
        closed_coords = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
        res, was_mod = GeometrySanitizer.ensure_ring_closed(closed_coords)

        assert was_mod is False
        assert len(res) == 5
        assert res == closed_coords

    def test_duplicate_vertex_removal(self):
        # Coordinates with consecutive identical vertices
        coords_with_dups = [
            (0.0, 0.0),
            (0.0, 0.0),       # Duplicate 1
            (10.0, 0.0),
            (10.0, 0.0),      # Duplicate 2
            (10.0, 10.0),
            (0.0, 10.0),
            (0.0, 10.00001),  # Near-duplicate within tolerance
            (0.0, 0.0),
        ]
        cleaned, removed_count = GeometrySanitizer.remove_duplicate_vertices(
            coords_with_dups, tolerance=0.001
        )

        assert removed_count >= 2
        assert len(cleaned) == 5  # 4 rectangle vertices + closing vertex
        assert cleaned[0] == cleaned[-1]

    def test_collinear_vertex_decimation(self):
        # Straight bottom edge with 3 redundant intermediate collinear points
        coords = [
            (0.0, 0.0),
            (2.5, 0.0),   # Redundant collinear
            (5.0, 0.0),   # Redundant collinear
            (7.5, 0.0),   # Redundant collinear
            (10.0, 0.0),
            (10.0, 10.0),
            (0.0, 10.0),
            (0.0, 0.0),
        ]
        decimated, removed_count = GeometrySanitizer.remove_collinear_vertices(
            coords, tolerance=0.005
        )

        assert removed_count == 3
        assert len(decimated) == 5  # (0,0), (10,0), (10,10), (0,10), (0,0)
        assert (5.0, 0.0) not in decimated

    def test_acute_spike_removal(self):
        # Rectangle with an overshoot needle spike along the top edge
        # Path goes (0,10) -> (5,10) -> (5, 10.03) -> (5,10) -> (10,10)
        coords_with_spike = [
            (0.0, 0.0),
            (10.0, 0.0),
            (10.0, 10.0),
            (5.0, 10.0),
            (5.0, 10.02),  # 2 cm needle spike
            (5.0, 10.0),
            (0.0, 10.0),
            (0.0, 0.0),
        ]
        filtered, removed_count = GeometrySanitizer.remove_spikes(
            coords_with_spike, max_angle_deg=2.0, spike_length_tolerance=0.05
        )

        assert removed_count >= 1
        assert (5.0, 10.02) not in filtered

    def test_self_intersecting_bowtie_repair(self):
        # Classic figure-8 bow-tie self-intersecting polygon
        bowtie = Polygon([(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0), (0.0, 0.0)])
        assert not bowtie.is_valid

        result = GeometrySanitizer.clean_polygon(bowtie)

        assert isinstance(result, CadastralNormalizationResult)
        assert result.geometry.is_valid is True
        assert result.geometry.area > 0.0
        assert "INVALID_GEOMETRY" in result.audit.repaired_defects

    def test_ring_orientation_ogc_rfc7946(self):
        # Clockwise exterior shell
        cw_exterior = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)]
        poly_cw = Polygon(cw_exterior)
        assert poly_cw.exterior.is_ccw is False

        res = GeometrySanitizer.clean_polygon(poly_cw)

        assert res.geometry.exterior.is_ccw is True
        assert res.geometry.area == pytest.approx(100.0)
        assert "WRONG_ORIENTATION" in res.audit.repaired_defects

    def test_3d_z_coordinate_stripping(self):
        # 3D polygon with elevation (Z) ordinates
        poly_3d = Polygon([(0, 0, 500), (10, 0, 505), (10, 10, 510), (0, 10, 502)])
        assert poly_3d.has_z is True

        res = GeometrySanitizer.clean_polygon(poly_3d)

        assert res.geometry.has_z is False
        assert res.geometry.area == pytest.approx(100.0)

    def test_empty_or_none_geometry_raises(self):
        with pytest.raises(NormalizationError, match="null or empty"):
            GeometrySanitizer.clean_polygon(Polygon())

        with pytest.raises(NormalizationError, match="null or empty"):
            GeometrySanitizer.clean_polygon(None)

    def test_degenerate_zero_area_collapsed_geometry_raises(self):
        # Collinear line disguised as a polygon
        collinear_poly = Polygon([(0.0, 0.0), (5.0, 5.0), (10.0, 10.0), (0.0, 0.0)])
        with pytest.raises(NormalizationError, match="non-polygonal|zero area"):
            GeometrySanitizer.clean_polygon(collinear_poly)


# =====================================================================
# 2. Cadastral Boundary Normalization Tests
# =====================================================================

class TestCadastralBoundaryNormalization:
    """Tests for parcel-level boundary normalization and audit trail tracking."""

    @pytest.fixture
    def sample_clean_khasra(self) -> Dict[str, Any]:
        fixtures = generate_clean_cadastral_fixtures()
        return fixtures[0]  # KHASRA-101 (40m x 50m = 2000 sqm)

    def test_clean_khasra_parcel_normalization(self, sample_clean_khasra):
        geom: Polygon = sample_clean_khasra["geometry_everest"]
        res = GeometrySanitizer.clean_polygon(geom)

        assert isinstance(res, CadastralNormalizationResult)
        assert res.geometry.is_valid is True
        assert res.geometry.area == pytest.approx(2000.0, abs=1e-3)
        assert res.audit.area_drift_pct == pytest.approx(0.0, abs=1e-6)
        assert res.audit.vertices_removed == 0
        assert res.audit.is_valid_polygon is True
        assert res.audit.is_multipolygon is False
        assert len(res.audit.repaired_defects) == 0

    def test_precision_grid_alignment(self):
        # Coordinate slightly off a 1mm grid
        imprecise_poly = Polygon([
            (3600000.0003, 500000.0007),
            (3600040.0002, 500000.0004),
            (3600040.0008, 500050.0001),
            (3600000.0004, 500050.0009),
            (3600000.0003, 500000.0007),
        ])
        config = NormalizationConfig(snap_precision_m=0.001)
        res = GeometrySanitizer.clean_polygon(imprecise_poly, config=config)

        # All coordinates must be rounded to 3 decimal places (mm)
        for x, y in res.geometry.exterior.coords:
            assert pytest.approx(x, abs=1e-5) == round(x, 3)
            assert pytest.approx(y, abs=1e-5) == round(y, 3)

    def test_parcel_with_interior_holes(self):
        # 100m x 100m parcel with a 20m x 20m interior tank/pond hole
        exterior = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
        hole = [(40, 40), (60, 40), (60, 60), (40, 60), (40, 40)]  # CCW hole
        poly = Polygon(exterior, [hole])

        res = GeometrySanitizer.clean_polygon(poly)

        assert res.geometry.is_valid is True
        assert len(res.geometry.interiors) == 1
        # Net area: 10000 - 400 = 9600 sqm
        assert res.geometry.area == pytest.approx(9600.0)
        assert res.audit.num_holes == 1
        # Interior hole must be oriented clockwise (not CCW)
        assert res.geometry.interiors[0].is_ccw is False

    def test_multipolygon_normalization(self):
        # MultiPolygon representing disjoint parcel shares
        p1 = box(0, 0, 10, 10)
        p2 = box(20, 20, 30, 30)
        mp = MultiPolygon([p1, p2])

        res = GeometrySanitizer.clean_polygon(mp)

        assert isinstance(res.geometry, MultiPolygon)
        assert res.audit.is_multipolygon is True
        assert res.audit.num_parts == 2
        assert res.geometry.area == pytest.approx(200.0)

    def test_normalization_audit_to_dict(self, sample_clean_khasra):
        geom = sample_clean_khasra["geometry_everest"]
        res = GeometrySanitizer.clean_polygon(geom)
        audit_dict = res.audit.to_dict()

        assert "original_area_sqm" in audit_dict
        assert "normalized_area_sqm" in audit_dict
        assert "area_drift_pct" in audit_dict
        assert "original_vertex_count" in audit_dict
        assert "normalized_vertex_count" in audit_dict
        assert "max_displacement_m" in audit_dict
        assert "is_valid_polygon" in audit_dict
        assert audit_dict["is_valid_polygon"] is True


# =====================================================================
# 3. Topology Preservation & Snapping Tests
# =====================================================================

class TestTopologyPreservationAndSnapping:
    """Tests for edge alignment, vertex snapping, and legal area preservation between parcels."""

    def test_legal_area_preservation_verification(self):
        poly_orig = Polygon([(0, 0), (50, 0), (50, 40), (0, 40), (0, 0)])  # 2000 sqm
        # Sanitized with tiny 0.05 sqm shift (e.g. from 1mm coordinate rounding)
        poly_san = Polygon([(0, 0), (50.001, 0), (50.001, 40), (0, 40), (0, 0)])

        preserved, drift_pct, reason = CadastralTopologyPreserver.verify_legal_area_preservation(
            poly_orig, poly_san, max_drift_pct=0.05
        )

        assert preserved is True
        assert drift_pct < 0.01
        assert "Area preserved" in reason

    def test_legal_area_preservation_fails_on_excessive_drift(self):
        poly_orig = Polygon([(0, 0), (50, 0), (50, 40), (0, 40), (0, 0)])  # 2000 sqm
        # 50 sqm loss (2.5% drift)
        poly_distorted = Polygon([(0, 0), (48.75, 0), (48.75, 40), (0, 40), (0, 0)])

        preserved, drift_pct, reason = CadastralTopologyPreserver.verify_legal_area_preservation(
            poly_orig, poly_distorted, max_drift_pct=0.05, max_drift_sqm=0.5
        )

        assert preserved is False
        assert drift_pct > 0.05
        assert "exceeds allowable threshold" in reason

    def test_shared_boundary_snapping_adjacent_parcels(self):
        # Two contiguous parcels: Parcel A [0, 40] and Parcel B [40, 90]
        # Introduce a 2 cm micro-gap jitter on parcel B's western boundary
        poly_a = Polygon([(0, 0), (40, 0), (40, 50), (0, 50), (0, 0)])
        poly_b_jittered = Polygon([(40.02, 0), (90, 0), (90, 50), (40.02, 50), (40.02, 0)])

        # Before snapping, they do not touch and have a 2cm gap
        assert poly_a.touches(poly_b_jittered) is False
        assert poly_a.distance(poly_b_jittered) == pytest.approx(0.02, abs=1e-4)

        snapped_a, snapped_b = CadastralTopologyPreserver.snap_parcel_pair(
            poly_a, poly_b_jittered, tolerance=0.05
        )

        # After snapping within 5cm tolerance, shared boundaries align
        assert snapped_a.is_valid is True
        assert snapped_b.is_valid is True
        assert snapped_a.distance(snapped_b) == pytest.approx(0.0, abs=1e-5)
        # Intersection is a 1D shared boundary line, zero area
        assert snapped_a.intersection(snapped_b).area == pytest.approx(0.0, abs=1e-6)

    def test_align_shared_boundaries_collection(self):
        # Collection of 3 contiguous parcels in a row
        parcels = [
            {"parcel_identifier": "P1", "geometry": box(0, 0, 40, 50)},
            {"parcel_identifier": "P2", "geometry": box(40.01, 0, 90, 50)},    # 1 cm gap with P1
            {"parcel_identifier": "P3", "geometry": box(90.015, 0, 130, 50)},  # 1.5 cm gap with P2
        ]
        aligned = CadastralTopologyPreserver.align_shared_boundaries(
            parcels, tolerance=0.05
        )

        assert len(aligned) == 3
        geom1 = aligned[0]["geometry"]
        geom2 = aligned[1]["geometry"]
        geom3 = aligned[2]["geometry"]

        assert geom1.distance(geom2) == pytest.approx(0.0, abs=1e-4)
        assert geom2.distance(geom3) == pytest.approx(0.0, abs=1e-4)


# =====================================================================
# 4. CadastralTopologyValidator Tests
# =====================================================================

class TestCadastralTopologyValidator:
    """Tests for planar graph validation and topological anomaly classification."""

    def test_polsby_popper_compactness_calculation(self):
        # A circle has maximum compactness (~1.0)
        circle = Point(0, 0).buffer(10.0, quad_segs=16)
        c_circle = calculate_polsby_popper_compactness(circle)
        assert c_circle > 0.95

        # A standard rectangle 40m x 50m
        rect = box(0, 0, 40, 50)
        c_rect = calculate_polsby_popper_compactness(rect)
        # Perimeter = 180, Area = 2000, 4*pi*2000 / 180^2 ~ 0.775
        assert 0.70 <= c_rect <= 0.85

        # A long thin sliver 50m x 0.05m
        sliver = box(0, 0, 50, 0.05)
        c_sliver = calculate_polsby_popper_compactness(sliver)
        # Extremely low compactness
        assert c_sliver < 0.02

    def test_single_parcel_sliver_detection(self):
        sliver_dict = create_sliver_polygon(base_x=0.0, base_y=0.0, length=40.0, width=0.05)
        anomalies = CadastralTopologyValidator.validate_single_parcel(
            "SLIVER-01", sliver_dict["geometry_everest"]
        )

        assert len(anomalies) >= 1
        assert any(a.defect_type == DefectType.SLIVER for a in anomalies)
        assert any(a.severity == DefectSeverity.WARNING for a in anomalies)

    def test_micro_overlap_classification_auto_resolvable(self):
        # 0.05m overlap * 50m height = 2.5 sqm (< 5.0 sqm threshold)
        p1 = box(0, 0, 40.05, 50)
        p2 = box(40, 0, 90, 50)

        anomalies = CadastralTopologyValidator.validate_parcel_pair("P1", p1, "P2", p2)

        assert len(anomalies) == 1
        anom = anomalies[0]
        assert anom.defect_type == DefectType.OVERLAP
        assert anom.severity == DefectSeverity.AUTO_RESOLVABLE
        assert anom.affected_area_sqm == pytest.approx(2.5, abs=1e-3)
        assert "P1" in anom.affected_parcel_ids
        assert "P2" in anom.affected_parcel_ids

    def test_critical_dispute_overlap_classification(self):
        # 2.5m overlap * 50m height = 125.0 sqm (> 5.0 sqm threshold)
        p1 = box(0, 0, 42.5, 50)
        p2 = box(40, 0, 90, 50)

        anomalies = CadastralTopologyValidator.validate_parcel_pair("P1", p1, "P2", p2)

        assert len(anomalies) == 1
        anom = anomalies[0]
        assert anom.defect_type == DefectType.OVERLAP
        assert anom.severity == DefectSeverity.CRITICAL_DISPUTE
        assert anom.affected_area_sqm == pytest.approx(125.0, abs=1e-3)

    def test_dangling_node_detection(self):
        # Parcel A has an extra node at (40, 25) along the shared boundary
        p_a = Polygon([(0, 0), (40, 0), (40, 25), (40, 50), (0, 50), (0, 0)])
        # Parcel B does NOT have node (40, 25)
        p_b = Polygon([(40, 0), (90, 0), (90, 50), (40, 50), (40, 0)])

        dangling = CadastralTopologyValidator.detect_dangling_nodes(p_a, p_b)

        assert len(dangling) == 1
        assert dangling[0] == (40.0, 25.0)

    def test_internal_boundary_gap_detection(self):
        # 4 parcels surrounding a central 5m x 5m courtyard gap
        # Left-Bottom [0, 40] x [0, 40]
        # Right-Bottom [45, 85] x [0, 40]
        # Left-Top [0, 40] x [45, 85]
        # Right-Top [45, 85] x [45, 85]
        # Between them lies a void from x:[40,45], y:[40,45] enclosed by surrounding parcels
        # Create an enclosed collar around the 5x5 gap
        p_outer = box(0, 0, 50, 50)
        p_gap = box(20, 20, 25, 25)  # 25 sqm gap inside
        p_doughnut = p_outer.difference(p_gap)

        # Split doughnut into 2 parcels surrounding the gap
        p1 = p_doughnut.intersection(box(0, 0, 25, 50))
        p2 = p_doughnut.intersection(box(25, 0, 50, 50))

        parcels = [
            {"parcel_identifier": "P1", "geometry": p1},
            {"parcel_identifier": "P2", "geometry": p2},
        ]
        gaps = CadastralTopologyValidator.find_internal_boundary_gaps(parcels)

        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.defect_type == DefectType.GAP
        assert gap.affected_area_sqm == pytest.approx(25.0, abs=1e-3)

    def test_clean_village_cadastral_block_zero_defects(self):
        # Clean 10-parcel cadastral village block (Khasra 101-110)
        clean_parcels = generate_clean_cadastral_fixtures()
        report = CadastralTopologyValidator.validate_parcel_collection(
            clean_parcels,
            geom_key="geometry_everest",
            id_key="parcel_identifier",
        )

        assert isinstance(report, CadastralTopologyReport)
        assert report.total_parcels == 10
        assert report.valid_parcels_count == 10
        assert report.invalid_parcels_count == 0
        assert report.is_planar_graph_valid is True
        assert report.total_overlap_area_sqm == pytest.approx(0.0)
        assert report.total_gap_area_sqm == pytest.approx(0.0)
        assert report.auto_resolvable_count == 0
        assert report.critical_dispute_count == 0
        assert len(report.anomalies) == 0


# =====================================================================
# 5. Deterministic Cadastral Fixture Integration Tests
# =====================================================================

class TestDefectiveFixtureDiagnostics:
    """Tests evaluating defect detection against the project's standard defect fixtures."""

    def test_injected_micro_overlap_diagnostics(self):
        clean_parcels = generate_clean_cadastral_fixtures()
        p101 = clean_parcels[0]
        p102 = clean_parcels[1]

        mod_a, p_b, defect_meta = inject_micro_overlap(p101, p102, overlap_distance=0.05)

        test_parcels = [mod_a, p_b]
        report = CadastralTopologyValidator.validate_parcel_collection(
            test_parcels,
            geom_key="geometry_everest",
            id_key="parcel_identifier",
        )

        assert report.is_planar_graph_valid is False
        assert report.total_overlap_area_sqm == pytest.approx(2.5, abs=1e-3)
        assert report.auto_resolvable_count == 1
        assert report.critical_dispute_count == 0

        # Check anomaly details
        overlap_anom = [a for a in report.anomalies if a.defect_type == DefectType.OVERLAP][0]
        assert overlap_anom.severity == DefectSeverity.AUTO_RESOLVABLE
        assert "KHASRA-101" in overlap_anom.affected_parcel_ids
        assert "KHASRA-102" in overlap_anom.affected_parcel_ids

    def test_injected_critical_dispute_diagnostics(self):
        clean_parcels = generate_clean_cadastral_fixtures()
        p101 = clean_parcels[0]
        p102 = clean_parcels[1]

        mod_a, p_b, defect_meta = inject_critical_dispute_overlap(p101, p102, encroachment_distance=2.5)

        test_parcels = [mod_a, p_b]
        report = CadastralTopologyValidator.validate_parcel_collection(
            test_parcels,
            geom_key="geometry_everest",
            id_key="parcel_identifier",
        )

        assert report.is_planar_graph_valid is False
        assert report.total_overlap_area_sqm == pytest.approx(125.0, abs=1e-3)
        assert report.auto_resolvable_count == 0
        assert report.critical_dispute_count == 1

        overlap_anom = [a for a in report.anomalies if a.defect_type == DefectType.OVERLAP][0]
        assert overlap_anom.severity == DefectSeverity.CRITICAL_DISPUTE

    def test_injected_boundary_gap_diagnostics(self):
        clean_parcels = generate_clean_cadastral_fixtures()
        p101 = clean_parcels[0]
        p102 = clean_parcels[1]

        mod_a, p_b, defect_meta = inject_boundary_gap(p101, p102, gap_distance=0.5)

        # Before gap injection they touched; after gap injection they no longer intersect
        assert mod_a["geometry_everest"].intersects(p_b["geometry_everest"]) is False
        assert mod_a["geometry_everest"].distance(p_b["geometry_everest"]) == pytest.approx(0.5, abs=1e-4)

    def test_full_defective_dataset_comprehensive_audit(self):
        # Generate complete defective test suite with overlaps, gaps, and slivers
        parcels, defects_manifest = generate_defective_cadastral_dataset()

        report = CadastralTopologyValidator.validate_parcel_collection(
            parcels,
            geom_key="geometry_everest",
            id_key="parcel_identifier",
        )

        # Defective dataset must fail planar graph validity
        assert report.is_planar_graph_valid is False
        assert report.total_parcels == len(parcels)
        assert len(report.anomalies) > 0

        # Anomalies must detect overlaps and slivers
        defect_types = {a.defect_type for a in report.anomalies}
        assert DefectType.OVERLAP in defect_types
        assert DefectType.SLIVER in defect_types

        # Verify serializability of report
        report_dict = report.to_dict()
        assert isinstance(report_dict, dict)
        assert report_dict["is_planar_graph_valid"] is False
        assert "anomalies" in report_dict
        assert len(report_dict["anomalies"]) == len(report.anomalies)
