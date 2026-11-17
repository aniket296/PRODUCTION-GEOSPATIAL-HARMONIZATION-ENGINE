"""Phase 5 Verification: Property-based invariants, idempotence, determinism, and memory/runtime scaling."""

from __future__ import annotations

import time
import tracemalloc
import uuid

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon

from backend.export.flatgeobuf import export_parcels_to_flatgeobuf
from backend.export.geojson import export_parcels_to_geojson_bytes
from backend.healing.orchestrator import execute_layer_healing_pipeline
from backend.models.parcel import HarmonizedParcel, ParcelResolutionStatus
from backend.normalizer.geometry import normalize_to_2d_multipolygon
from backend.topology.recorder import project_metric_geometry_to_wgs84

DX, DY = 500_000.0, 3_000_000.0


def _make_grid_parcel(layer_id: uuid.UUID, idx: int, row: int, col: int) -> HarmonizedParcel:
    x0 = DX + col * 50.0
    y0 = DY + row * 50.0
    poly = Polygon([(x0, y0), (x0 + 50.0, y0), (x0 + 50.0, y0 + 50.0), (x0, y0 + 50.0), (x0, y0)])
    mp = MultiPolygon([poly])
    return HarmonizedParcel(
        id=uuid.UUID(int=idx + 1),
        layer_id=layer_id,
        parcel_identifier=f"P-{row}-{col}",
        geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(mp), srid=4326),
        geometry_projected=from_shape(mp, srid=32643),
        metric_area_sqm=float(poly.area),
        metric_perimeter_m=float(poly.length),
        polsby_popper_compactness=HarmonizedParcel.calculate_polsby_popper(float(poly.area), float(poly.length)),
        resolution_status=ParcelResolutionStatus.ORIGINAL.value,
    )


class TestPropertiesAndPerformance:
    """Property-based invariants (idempotence, determinism, area conservation) and scaling benchmarks."""

    def test_normalize_to_2d_multipolygon_is_idempotent_and_strips_z(self) -> None:
        poly_3d = Polygon([(0.0, 0.0, 15.0), (0.0, 20.0, 15.0), (20.0, 20.0, 15.0), (20.0, 0.0, 15.0), (0.0, 0.0, 15.0)])
        once = normalize_to_2d_multipolygon(poly_3d)
        twice = normalize_to_2d_multipolygon(once)

        assert not once.has_z
        assert once.equals_exact(twice, tolerance=0.0)
        assert list(once.geoms[0].exterior.coords) == list(twice.geoms[0].exterior.coords)

    def test_healing_pipeline_is_idempotent_and_preserves_total_area(self) -> None:
        layer_id = uuid.uuid4()
        p_main = _make_grid_parcel(layer_id, 1, 0, 0)
        # Attach a 2.5 m^2 sliver sharing a 50 m boundary with p_main
        sliver_poly = Polygon(
            [
                (DX - 0.05, DY),
                (DX, DY),
                (DX, DY + 50.0),
                (DX - 0.05, DY + 50.0),
                (DX - 0.05, DY),
            ]
        )
        sliver_mp = MultiPolygon([sliver_poly])
        p_sliver = HarmonizedParcel(
            id=uuid.UUID(int=99),
            layer_id=layer_id,
            parcel_identifier="P-SLIVER",
            geometry_wgs84=from_shape(project_metric_geometry_to_wgs84(sliver_mp), srid=4326),
            geometry_projected=from_shape(sliver_mp, srid=32643),
            metric_area_sqm=float(sliver_poly.area),
            metric_perimeter_m=float(sliver_poly.length),
            is_sliver=True,
            resolution_status=ParcelResolutionStatus.ORIGINAL.value,
        )

        initial_total_area = float(p_main.metric_area_sqm or 0.0) + float(p_sliver.metric_area_sqm or 0.0)

        res1 = execute_layer_healing_pipeline([p_main, p_sliver])
        assert len(res1.sliver_result.assignments) == 1
        assert float(p_main.metric_area_sqm or 0.0) == pytest.approx(initial_total_area, abs=1e-3)

        # Running healing a second time on active parcels is a no-op (idempotent)
        active = [p for p in (p_main, p_sliver) if not (p.attributes or {}).get("merged_into_parcel_id")]
        wkb_after_first = bytes(p_main.geometry_projected.data)
        res2 = execute_layer_healing_pipeline(active)
        assert len(res2.sliver_result.assignments) == 0
        assert bytes(p_main.geometry_projected.data) == wkb_after_first

    def test_performance_and_memory_scaling_100_parcels(self) -> None:
        layer_id = uuid.uuid4()
        parcels = [
            _make_grid_parcel(layer_id, r * 10 + c, r, c)
            for r in range(10)
            for c in range(10)
        ]

        tracemalloc.start()
        t0 = time.perf_counter()
        geojson_bytes = export_parcels_to_geojson_bytes(parcels)
        fgb_bytes = export_parcels_to_flatgeobuf(parcels, layer_name="perf_100")
        elapsed_s = time.perf_counter() - t0
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert len(geojson_bytes) > 10_000
        assert len(fgb_bytes) > 5_000
        assert elapsed_s < 5.0
        assert peak_bytes < 50 * 1024 * 1024  # < 50 MB peak memory
