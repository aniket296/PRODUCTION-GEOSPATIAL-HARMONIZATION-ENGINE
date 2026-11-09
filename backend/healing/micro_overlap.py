"""Deterministic micro-overlap centerline splitting and layer healing (Step 93/150).

Directive Sections 15, 20-23, 25:
- Pairwise overlaps where BOTH independent parcel overlap percentages are within `max_overlap_percent_threshold`
  (default <= 2.0%) and neither parcel is `dispute_locked` are classified as `AUTO_RESOLVABLE` micro-overlaps.
- Each micro-overlap is resolved deterministically in projected metric coordinates (`EPSG:32643`) by bisecting the
  intersection polygon (`A ∩ B`) along a straight centerline perpendicular to the separation axis between `A - B` and
  `B - A` so that each parcel receives exactly 50% of the overlap area with zero residual overlap and zero gap.
- Overlaps exceeding the threshold on either parcel (`CRITICAL_DISPUTE`) or involving a `dispute_locked` parcel are
  never modified and are reported in `skipped_critical_pairs`.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from geoalchemy2.shape import from_shape, to_shape
from shapely import wkt as shapely_wkt
from shapely.geometry import GeometryCollection, MultiLineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

from backend.core.config import get_settings
from backend.healing.shared_boundary import extract_shared_boundary_lines
from backend.healing.sliver_union import NeighborMetrics
from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologyErrorType, TopologySeverity
from backend.topology.compactness import evaluate_geometry_compactness
from backend.topology.intersection import PROJECTED_SRID, extract_polygonal_intersection
from backend.topology.overlap_percentage import evaluate_pair_overlap_metrics
from backend.topology.recorder import project_metric_geometry_to_wgs84
from backend.topology.severity import classify_topology_severity

_BISECTION_ITERATIONS = 64


@dataclass(frozen=True, slots=True)
class MicroOverlapCandidateInput:
    """A parcel candidate considered for batch micro-overlap healing."""

    parcel_id: uuid.UUID
    geometry: BaseGeometry | str
    dispute_locked: bool = False


@dataclass(frozen=True, slots=True)
class MicroOverlapSplitResult:
    """Deterministic outcome of splitting a single pairwise micro-overlap along its centerline."""

    parcel_a_id: uuid.UUID
    parcel_b_id: uuid.UUID
    intersection_area_sqm: float
    overlap_percentage_a: float
    overlap_percentage_b: float
    centerline_geometry: MultiLineString
    geometry_a_after: MultiPolygon
    geometry_b_after: MultiPolygon
    area_a_before_sqm: float
    area_a_after_sqm: float
    area_b_before_sqm: float
    area_b_after_sqm: float


@dataclass(frozen=True, slots=True)
class MicroOverlapBatchResult:
    """Outcome of batch micro-overlap resolution across a collection of parcels."""

    resolved_pairs: tuple[MicroOverlapSplitResult, ...]
    skipped_critical_pairs: tuple[tuple[uuid.UUID, uuid.UUID], ...]
    updated_geometries: dict[uuid.UUID, MultiPolygon]
    updated_metrics: dict[uuid.UUID, NeighborMetrics]


def _coerce_geometry(geom: BaseGeometry | str) -> BaseGeometry:
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    if isinstance(geom, BaseGeometry):
        return geom
    raise TypeError(f"Expected Shapely BaseGeometry or WKT string, got {type(geom).__name__}")


def _to_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    valid = geom if geom.is_valid else make_valid(geom)
    polygons: list[Polygon] = []

    def _collect(part: BaseGeometry) -> None:
        if part.is_empty:
            return
        if isinstance(part, Polygon):
            if part.area > 0.0:
                polygons.append(part)
        elif isinstance(part, (MultiPolygon, GeometryCollection)):
            for child in part.geoms:
                _collect(child)

    _collect(valid)
    mp = MultiPolygon(polygons)
    if not mp.is_valid:
        repaired = make_valid(mp)
        polygons.clear()
        _collect(repaired)
        mp = MultiPolygon(polygons)
    return mp


def _separation_unit_vector(
    geom_a: BaseGeometry,
    geom_b: BaseGeometry,
    only_a: BaseGeometry,
    only_b: BaseGeometry,
) -> tuple[float, float]:
    """Compute a deterministic unit separation vector pointing from parcel A toward parcel B."""
    ref_a = only_a if not only_a.is_empty else geom_a
    ref_b = only_b if not only_b.is_empty else geom_b
    ca = ref_a.centroid
    cb = ref_b.centroid
    dx = float(cb.x - ca.x)
    dy = float(cb.y - ca.y)
    norm = math.hypot(dx, dy)
    if norm <= 1e-12:
        return (1.0, 0.0)
    return (dx / norm, dy / norm)


def _half_plane_polygon(
    ux: float,
    uy: float,
    t: float,
    origin_x: float,
    origin_y: float,
    span: float,
) -> Polygon:
    """Build a large half-plane polygon `{(x, y) : ux * (x - origin_x) + uy * (y - origin_y) <= t}`."""
    # Tangent vector perpendicular to (ux, uy)
    tx, ty = -uy, ux
    # Point on the cutting line: P0 = origin + t * u
    px = origin_x + t * ux
    py = origin_y + t * uy
    # Far point in the -u direction
    bx = px - span * ux
    by = py - span * uy

    p1 = (px - span * tx, py - span * ty)
    p2 = (px + span * tx, py + span * ty)
    p3 = (bx + span * tx, by + span * ty)
    p4 = (bx - span * tx, by - span * ty)
    return Polygon([p1, p2, p3, p4, p1])


def _bisect_overlap_polygon(
    overlap_mp: MultiPolygon,
    ux: float,
    uy: float,
) -> tuple[MultiPolygon, MultiPolygon, MultiLineString]:
    """Bisect `overlap_mp` into two equal-area MultiPolygons perpendicular to `(ux, uy)`."""
    minx, miny, maxx, maxy = overlap_mp.bounds
    origin_x = 0.5 * (minx + maxx)
    origin_y = 0.5 * (miny + maxy)
    diag = math.hypot(maxx - minx, maxy - miny)
    span = max(diag * 4.0, 100.0)

    corners = [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]
    projections = [ux * (x - origin_x) + uy * (y - origin_y) for x, y in corners]
    lo = min(projections) - 1e-6
    hi = max(projections) + 1e-6

    target_area = 0.5 * float(overlap_mp.area)
    for _ in range(_BISECTION_ITERATIONS):
        mid = 0.5 * (lo + hi)
        hp = _half_plane_polygon(ux, uy, mid, origin_x, origin_y, span)
        area_left = float(overlap_mp.intersection(hp).area)
        if area_left < target_area:
            lo = mid
        else:
            hi = mid

    cut_t = 0.5 * (lo + hi)
    cut_hp = _half_plane_polygon(ux, uy, cut_t, origin_x, origin_y, span)
    part_a = _to_multipolygon(overlap_mp.intersection(cut_hp))
    part_b = _to_multipolygon(overlap_mp.difference(cut_hp))
    centerline = extract_shared_boundary_lines(part_a, part_b)
    return part_a, part_b, centerline


def resolve_pair_micro_overlap(
    parcel_a_id: uuid.UUID,
    geom_a: BaseGeometry | str,
    parcel_b_id: uuid.UUID,
    geom_b: BaseGeometry | str,
    *,
    threshold_percent: float | None = None,
    involves_dispute_locked: bool = False,
    area_tolerance_sqm: float = 1e-4,
) -> MicroOverlapSplitResult:
    """Deterministically split a pairwise micro-overlap along its centerline.

    Canonical ordering (`parcel_a_id < parcel_b_id`) is enforced automatically so the result is invariant to argument
    order.

    Raises:
        ValueError: If `parcel_a_id == parcel_b_id`, if the pair has no polygonal overlap, if the overlap classifies
            as `CRITICAL_DISPUTE`, or if area conservation is violated.
    """
    if parcel_a_id == parcel_b_id:
        raise ValueError("Cannot resolve micro-overlap of a parcel with itself")
    if not math.isfinite(area_tolerance_sqm) or area_tolerance_sqm < 0.0:
        raise ValueError("area_tolerance_sqm must be >= 0 and finite")

    poly_a = _coerce_geometry(geom_a)
    poly_b = _coerce_geometry(geom_b)

    if parcel_a_id > parcel_b_id:
        parcel_a_id, parcel_b_id = parcel_b_id, parcel_a_id
        poly_a, poly_b = poly_b, poly_a

    metrics = evaluate_pair_overlap_metrics(
        poly_a,
        poly_b,
        parcel_a_id=parcel_a_id,
        parcel_b_id=parcel_b_id,
    )
    if metrics.intersection_area_sqm <= 0.0:
        raise ValueError("Parcels have no polygonal overlap to resolve")

    threshold = (
        get_settings().max_overlap_percent_threshold if threshold_percent is None else float(threshold_percent)
    )
    severity = classify_topology_severity(
        TopologyErrorType.OVERLAP,
        metrics.overlap_percentage_primary,
        metrics.overlap_percentage_secondary,
        threshold_percent=threshold,
        involves_dispute_locked_parcel=involves_dispute_locked,
    )
    if severity is TopologySeverity.CRITICAL_DISPUTE:
        raise ValueError(
            f"Cannot auto-resolve CRITICAL_DISPUTE overlap between {parcel_a_id} and {parcel_b_id} "
            f"({metrics.overlap_percentage_primary:.4f}%, {metrics.overlap_percentage_secondary:.4f}%)"
        )

    overlap_mp = metrics.intersection_geometry
    only_a = _to_multipolygon(poly_a.difference(overlap_mp))
    only_b = _to_multipolygon(poly_b.difference(overlap_mp))

    ux, uy = _separation_unit_vector(poly_a, poly_b, only_a, only_b)
    half_a, half_b, centerline = _bisect_overlap_polygon(overlap_mp, ux, uy)

    healed_a = _to_multipolygon(unary_union([only_a, half_a]))
    healed_b = _to_multipolygon(unary_union([only_b, half_b]))

    union_area_before = float(unary_union([poly_a, poly_b]).area)
    area_a_after = float(healed_a.area)
    area_b_after = float(healed_b.area)

    if abs((area_a_after + area_b_after) - union_area_before) > area_tolerance_sqm:
        raise ValueError(
            f"Area conservation violated splitting overlap between {parcel_a_id} and {parcel_b_id}: "
            f"expected {union_area_before:.6f} m², got {area_a_after + area_b_after:.6f} m²"
        )

    return MicroOverlapSplitResult(
        parcel_a_id=parcel_a_id,
        parcel_b_id=parcel_b_id,
        intersection_area_sqm=metrics.intersection_area_sqm,
        overlap_percentage_a=metrics.overlap_percentage_primary,
        overlap_percentage_b=metrics.overlap_percentage_secondary,
        centerline_geometry=centerline,
        geometry_a_after=healed_a,
        geometry_b_after=healed_b,
        area_a_before_sqm=metrics.area_a_sqm,
        area_a_after_sqm=area_a_after,
        area_b_before_sqm=metrics.area_b_sqm,
        area_b_after_sqm=area_b_after,
    )


def heal_micro_overlaps(
    candidates: Sequence[MicroOverlapCandidateInput],
    *,
    threshold_percent: float | None = None,
    area_tolerance_sqm: float = 1e-4,
) -> MicroOverlapBatchResult:
    """Deterministically resolve all eligible pairwise micro-overlaps across `candidates`.

    Pairs are evaluated in canonical ascending `(parcel_a_id, parcel_b_id)` order. Overlaps classified as
    `CRITICAL_DISPUTE` (or involving a `dispute_locked` parcel) are skipped and recorded in `skipped_critical_pairs`.
    """
    if not math.isfinite(area_tolerance_sqm) or area_tolerance_sqm < 0.0:
        raise ValueError("area_tolerance_sqm must be >= 0 and finite")

    threshold = (
        get_settings().max_overlap_percent_threshold if threshold_percent is None else float(threshold_percent)
    )

    sorted_cands = sorted(candidates, key=lambda c: c.parcel_id)
    current_geoms: dict[uuid.UUID, BaseGeometry] = {
        c.parcel_id: _coerce_geometry(c.geometry) for c in sorted_cands
    }
    locked_by_id: dict[uuid.UUID, bool] = {
        c.parcel_id: bool(c.dispute_locked) for c in sorted_cands
    }

    resolved_pairs: list[MicroOverlapSplitResult] = []
    skipped_critical_pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    updated_geometries: dict[uuid.UUID, MultiPolygon] = {}

    n = len(sorted_cands)
    for i in range(n):
        id_a = sorted_cands[i].parcel_id
        for j in range(i + 1, n):
            id_b = sorted_cands[j].parcel_id
            geom_a = current_geoms[id_a]
            geom_b = current_geoms[id_b]

            if not geom_a.intersects(geom_b):
                continue
            inter_mp = extract_polygonal_intersection(geom_a, geom_b)
            if inter_mp.is_empty or inter_mp.area <= 0.0:
                continue

            involves_locked = locked_by_id[id_a] or locked_by_id[id_b]
            metrics = evaluate_pair_overlap_metrics(geom_a, geom_b, parcel_a_id=id_a, parcel_b_id=id_b)
            severity = classify_topology_severity(
                TopologyErrorType.OVERLAP,
                metrics.overlap_percentage_primary,
                metrics.overlap_percentage_secondary,
                threshold_percent=threshold,
                involves_dispute_locked_parcel=involves_locked,
            )
            if severity is TopologySeverity.CRITICAL_DISPUTE:
                skipped_critical_pairs.append((id_a, id_b))
                continue

            split = resolve_pair_micro_overlap(
                id_a,
                geom_a,
                id_b,
                geom_b,
                threshold_percent=threshold,
                involves_dispute_locked=involves_locked,
                area_tolerance_sqm=area_tolerance_sqm,
            )
            current_geoms[id_a] = split.geometry_a_after
            current_geoms[id_b] = split.geometry_b_after
            updated_geometries[id_a] = split.geometry_a_after
            updated_geometries[id_b] = split.geometry_b_after
            resolved_pairs.append(split)

    updated_metrics: dict[uuid.UUID, NeighborMetrics] = {}
    for pid, mp in updated_geometries.items():
        comp = evaluate_geometry_compactness(mp)
        updated_metrics[pid] = NeighborMetrics(
            parcel_id=pid,
            area_sqm=float(comp.area_sqm),
            perimeter_m=float(comp.perimeter_m),
            polsby_popper=float(comp.polsby_popper),
        )

    return MicroOverlapBatchResult(
        resolved_pairs=tuple(resolved_pairs),
        skipped_critical_pairs=tuple(skipped_critical_pairs),
        updated_geometries=updated_geometries,
        updated_metrics=updated_metrics,
    )


def heal_layer_micro_overlaps(
    parcels: Sequence[HarmonizedParcel],
    *,
    threshold_percent: float | None = None,
    area_tolerance_sqm: float = 1e-4,
) -> MicroOverlapBatchResult:
    """Apply deterministic micro-overlap resolution in-place on `HarmonizedParcel` ORM records."""
    parcel_by_id: dict[uuid.UUID, HarmonizedParcel] = {p.id: p for p in parcels}
    candidates = [
        MicroOverlapCandidateInput(
            parcel_id=p.id,
            geometry=to_shape(p.geometry_projected),
            dispute_locked=bool(p.dispute_locked),
        )
        for p in parcels
    ]

    result = heal_micro_overlaps(
        candidates,
        threshold_percent=threshold_percent,
        area_tolerance_sqm=area_tolerance_sqm,
    )

    for pid, healed_mp in result.updated_geometries.items():
        parcel = parcel_by_id[pid]
        parcel.geometry_projected = from_shape(healed_mp, srid=PROJECTED_SRID)
        parcel.geometry_wgs84 = from_shape(project_metric_geometry_to_wgs84(healed_mp), srid=4326)
        parcel.recalculate_metrics(healed_mp)
        parcel.mark_auto_resolved()

    return result
