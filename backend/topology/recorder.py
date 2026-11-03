"""Topology conflict recording service and dual-CRS error persistence.

Coordinates all deterministic topology detectors (overlaps, slivers, boundary gaps, duplicate vertices,
and dangling nodes), classifies each anomaly via `classify_topology_severity`, updates parcel flags
(`is_sliver`, `is_disputed`, `dispute_locked`), projects conflict geometries from metric UTM Zone 43N
(`EPSG:32643`) to canonical WGS 84 (`EPSG:4326`), and persists `TopologicalError` records.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from pyproj import Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.parcel import HarmonizedParcel
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.topology.gap import DEFAULT_BOUNDARY_GAP_TOLERANCE_M, detect_layer_boundary_gaps
from backend.topology.intersection import PROJECTED_SRID
from backend.topology.overlap_percentage import evaluate_pair_overlap_metrics
from backend.topology.severity import DEFAULT_OVERLAP_THRESHOLD_PERCENT, classify_topology_severity
from backend.topology.sliver import (
    DEFAULT_SLIVER_MAX_AREA_SQM,
    DEFAULT_SLIVER_MAX_POLSBY_POPPER,
    detect_layer_slivers,
)
from backend.topology.vertices_and_nodes import (
    DEFAULT_DANGLING_SNAP_TOLERANCE_M,
    DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M,
    detect_dangling_nodes,
    detect_layer_duplicate_vertices,
)

WGS84_SRID: int = 4326


@lru_cache(maxsize=8)
def _get_transformer_to_wgs84(source_srid: int) -> Transformer:
    return Transformer.from_crs(f"EPSG:{source_srid}", f"EPSG:{WGS84_SRID}", always_xy=True)


def _coerce_shapely(geom: Any) -> BaseGeometry:
    if isinstance(geom, BaseGeometry):
        return geom
    if isinstance(geom, WKBElement):
        return to_shape(geom)
    if isinstance(geom, str):
        return shapely_wkt.loads(geom)
    raise TypeError(f"Unsupported geometry representation: {type(geom).__name__}")


def project_metric_geometry_to_wgs84(
    geom: BaseGeometry | WKBElement | str,
    source_srid: int = PROJECTED_SRID,
) -> BaseGeometry:
    """Project a metric geometry (default EPSG:32643) to canonical WGS 84 (EPSG:4326) longitude/latitude."""
    shape_geom = _coerce_shapely(geom)
    if shape_geom.is_empty:
        return shape_geom
    transformer = _get_transformer_to_wgs84(source_srid)
    return shapely_transform(transformer.transform, shape_geom)


@dataclass(frozen=True, slots=True)
class TopologyRecordingSummary:
    """Summary of recorded and persisted topological errors for a cadastral layer."""

    layer_id: uuid.UUID
    total_errors: int
    auto_resolvable_count: int
    critical_dispute_count: int
    warning_count: int
    counts_by_type: dict[str, int]
    errors: tuple[TopologicalError, ...]


class TopologyConflictRecorder:
    """Service that detects topological anomalies across a layer, locks disputed parcels, and persists errors."""

    def __init__(
        self,
        *,
        overlap_threshold_percent: float = DEFAULT_OVERLAP_THRESHOLD_PERCENT,
        sliver_max_area_sqm: float = DEFAULT_SLIVER_MAX_AREA_SQM,
        sliver_max_polsby_popper: float = DEFAULT_SLIVER_MAX_POLSBY_POPPER,
        gap_tolerance_m: float = DEFAULT_BOUNDARY_GAP_TOLERANCE_M,
        duplicate_vertex_tolerance_m: float = DEFAULT_DUPLICATE_VERTEX_TOLERANCE_M,
        dangling_snap_tolerance_m: float = DEFAULT_DANGLING_SNAP_TOLERANCE_M,
        projected_srid: int = PROJECTED_SRID,
    ) -> None:
        self.overlap_threshold_percent = overlap_threshold_percent
        self.sliver_max_area_sqm = sliver_max_area_sqm
        self.sliver_max_polsby_popper = sliver_max_polsby_popper
        self.gap_tolerance_m = gap_tolerance_m
        self.duplicate_vertex_tolerance_m = duplicate_vertex_tolerance_m
        self.dangling_snap_tolerance_m = dangling_snap_tolerance_m
        self.projected_srid = projected_srid

    def _dual_wkb(self, metric_geom: BaseGeometry) -> tuple[WKBElement, WKBElement]:
        wgs84_geom = project_metric_geometry_to_wgs84(metric_geom, source_srid=self.projected_srid)
        return (
            from_shape(wgs84_geom, srid=WGS84_SRID),
            from_shape(metric_geom, srid=self.projected_srid),
        )

    def build_layer_errors(
        self,
        layer_id: uuid.UUID,
        parcels: Sequence[HarmonizedParcel | tuple[uuid.UUID, BaseGeometry | str]],
    ) -> list[TopologicalError]:
        """Run all deterministic topology detectors on `parcels` and return `TopologicalError` ORM instances."""
        orm_by_id: dict[uuid.UUID, HarmonizedParcel] = {}
        geom_by_id: dict[uuid.UUID, BaseGeometry] = {}

        for item in parcels:
            if isinstance(item, HarmonizedParcel):
                pid = item.id
                geom = _coerce_shapely(item.geometry_projected)
                orm_by_id[pid] = item
                geom_by_id[pid] = geom
            else:
                pid, raw_geom = item
                geom_by_id[pid] = _coerce_shapely(raw_geom)

        sorted_pairs: list[tuple[uuid.UUID, BaseGeometry]] = sorted(
            geom_by_id.items(),
            key=lambda kv: kv[0],
        )

        def _is_locked(parcel_id: uuid.UUID | None) -> bool:
            if parcel_id is None:
                return False
            parcel_obj = orm_by_id.get(parcel_id)
            return bool(parcel_obj and parcel_obj.dispute_locked)

        errors: list[TopologicalError] = []

        # 1. Pairwise Overlaps
        n = len(sorted_pairs)
        for i in range(n):
            id_a, geom_a = sorted_pairs[i]
            for j in range(i + 1, n):
                id_b, geom_b = sorted_pairs[j]
                if id_a >= id_b or not geom_a.intersects(geom_b):
                    continue
                metrics = evaluate_pair_overlap_metrics(
                    geom_a=geom_a,
                    geom_b=geom_b,
                    parcel_a_id=id_a,
                    parcel_b_id=id_b,
                )
                if metrics.intersection_area_sqm <= 0.0 or metrics.intersection_geometry.is_empty:
                    continue

                severity = classify_topology_severity(
                    TopologyErrorType.OVERLAP,
                    metrics.overlap_percentage_primary,
                    metrics.overlap_percentage_secondary,
                    threshold_percent=self.overlap_threshold_percent,
                    involves_dispute_locked_parcel=_is_locked(id_a) or _is_locked(id_b),
                )
                if severity is TopologySeverity.CRITICAL_DISPUTE:
                    reason = (
                        f"Critical boundary overlap ({metrics.overlap_percentage_primary:.2f}% / "
                        f"{metrics.overlap_percentage_secondary:.2f}% > {self.overlap_threshold_percent:.2f}%)"
                    )
                    if id_a in orm_by_id:
                        orm_by_id[id_a].lock_dispute(reason)
                    if id_b in orm_by_id:
                        orm_by_id[id_b].lock_dispute(reason)

                wkb_wgs84, wkb_proj = self._dual_wkb(metrics.intersection_geometry)
                errors.append(
                    TopologicalError(
                        layer_id=layer_id,
                        primary_parcel_id=id_a,
                        secondary_parcel_id=id_b,
                        error_type=TopologyErrorType.OVERLAP.value,
                        severity=severity.value,
                        description=(
                            f"Pairwise overlap of {metrics.intersection_area_sqm:.4f} m² "
                            f"({metrics.overlap_percentage_primary:.2f}% primary, "
                            f"{metrics.overlap_percentage_secondary:.2f}% secondary)"
                        ),
                        overlap_percentage_primary=metrics.overlap_percentage_primary,
                        overlap_percentage_secondary=metrics.overlap_percentage_secondary,
                        affected_area_sqm=metrics.intersection_area_sqm,
                        error_geometry_wgs84=wkb_wgs84,
                        error_geometry_projected=wkb_proj,
                    )
                )

        # 2. Slivers
        for sliver in detect_layer_slivers(
            sorted_pairs,
            max_area_sqm=self.sliver_max_area_sqm,
            max_polsby_popper=self.sliver_max_polsby_popper,
        ):
            pid = sliver.parcel_id
            if pid is not None and pid in orm_by_id:
                orm_by_id[pid].is_sliver = True
            sliver_geom = geom_by_id[pid]  # type: ignore[index]
            severity = classify_topology_severity(
                TopologyErrorType.SLIVER,
                involves_dispute_locked_parcel=_is_locked(pid),
            )
            wkb_wgs84, wkb_proj = self._dual_wkb(sliver_geom)
            errors.append(
                TopologicalError(
                    layer_id=layer_id,
                    primary_parcel_id=pid,
                    secondary_parcel_id=None,
                    error_type=TopologyErrorType.SLIVER.value,
                    severity=severity.value,
                    description=(
                        f"Metric sliver parcel (area={sliver.area_sqm:.4f} m², "
                        f"Polsby-Popper={sliver.polsby_popper:.6f})"
                    ),
                    affected_area_sqm=sliver.area_sqm,
                    error_geometry_wgs84=wkb_wgs84,
                    error_geometry_projected=wkb_proj,
                )
            )

        # 3. Boundary Gaps
        for gap in detect_layer_boundary_gaps(sorted_pairs, tolerance_m=self.gap_tolerance_m):
            severity = classify_topology_severity(
                TopologyErrorType.GAP,
                involves_dispute_locked_parcel=_is_locked(gap.parcel_a_id) or _is_locked(gap.parcel_b_id),
            )
            wkb_wgs84, wkb_proj = self._dual_wkb(gap.gap_geometry)
            errors.append(
                TopologicalError(
                    layer_id=layer_id,
                    primary_parcel_id=gap.parcel_a_id,
                    secondary_parcel_id=gap.parcel_b_id,
                    error_type=TopologyErrorType.GAP.value,
                    severity=severity.value,
                    description=(
                        f"Boundary gap of {gap.gap_area_sqm:.4f} m² "
                        f"(min separation={gap.min_distance_m:.4f} m <= {gap.tolerance_m:.4f} m)"
                    ),
                    affected_area_sqm=gap.gap_area_sqm,
                    error_geometry_wgs84=wkb_wgs84,
                    error_geometry_projected=wkb_proj,
                )
            )

        # 4. Duplicate Vertices
        for dup in detect_layer_duplicate_vertices(
            sorted_pairs,
            tolerance_m=self.duplicate_vertex_tolerance_m,
        ):
            severity = classify_topology_severity(
                TopologyErrorType.DUPLICATE_VERTEX,
                involves_dispute_locked_parcel=_is_locked(dup.parcel_id),
            )
            wkb_wgs84, wkb_proj = self._dual_wkb(dup.point)
            errors.append(
                TopologicalError(
                    layer_id=layer_id,
                    primary_parcel_id=dup.parcel_id,
                    secondary_parcel_id=None,
                    error_type=TopologyErrorType.DUPLICATE_VERTEX.value,
                    severity=severity.value,
                    description=(
                        f"Duplicate vertex at part={dup.part_index}, ring={dup.ring_index}, "
                        f"vertex={dup.vertex_index} (distance={dup.distance_m:.6f} m)"
                    ),
                    affected_area_sqm=0.0,
                    error_geometry_wgs84=wkb_wgs84,
                    error_geometry_projected=wkb_proj,
                )
            )

        # 5. Dangling Nodes
        for node in detect_dangling_nodes(
            sorted_pairs,
            snap_tolerance_m=self.dangling_snap_tolerance_m,
        ):
            severity = classify_topology_severity(
                TopologyErrorType.DANGLING_NODE,
                involves_dispute_locked_parcel=(
                    _is_locked(node.parcel_id) or _is_locked(node.secondary_parcel_id)
                ),
            )
            wkb_wgs84, wkb_proj = self._dual_wkb(node.point)
            errors.append(
                TopologicalError(
                    layer_id=layer_id,
                    primary_parcel_id=node.parcel_id,
                    secondary_parcel_id=node.secondary_parcel_id,
                    error_type=TopologyErrorType.DANGLING_NODE.value,
                    severity=severity.value,
                    description=(
                        f"Dangling node ({node.node_kind}) with offset={node.gap_or_overshoot_m:.4f} m"
                    ),
                    affected_area_sqm=0.0,
                    error_geometry_wgs84=wkb_wgs84,
                    error_geometry_projected=wkb_proj,
                )
            )

        return errors

    async def persist_layer_conflicts(
        self,
        session: AsyncSession,
        layer_id: uuid.UUID,
        parcels: Sequence[HarmonizedParcel | tuple[uuid.UUID, BaseGeometry | str]] | None = None,
        *,
        replace_unresolved: bool = True,
    ) -> TopologyRecordingSummary:
        """Detect topological conflicts for `layer_id`, persist `TopologicalError` records, and flush `session`."""
        if parcels is None:
            stmt = (
                select(HarmonizedParcel)
                .where(HarmonizedParcel.layer_id == layer_id)
                .order_by(HarmonizedParcel.id)
            )
            result = await session.execute(stmt)
            parcels = list(result.scalars().all())

        if replace_unresolved:
            await session.execute(
                delete(TopologicalError).where(
                    TopologicalError.layer_id == layer_id,
                    TopologicalError.is_resolved.is_(False),
                )
            )

        errors = self.build_layer_errors(layer_id=layer_id, parcels=parcels)
        session.add_all(errors)
        await session.flush()

        counts_by_type = dict(Counter(e.error_type for e in errors))
        auto_count = sum(1 for e in errors if e.severity == TopologySeverity.AUTO_RESOLVABLE.value)
        critical_count = sum(1 for e in errors if e.severity == TopologySeverity.CRITICAL_DISPUTE.value)
        warning_count = sum(1 for e in errors if e.severity == TopologySeverity.WARNING.value)

        return TopologyRecordingSummary(
            layer_id=layer_id,
            total_errors=len(errors),
            auto_resolvable_count=auto_count,
            critical_dispute_count=critical_count,
            warning_count=warning_count,
            counts_by_type=counts_by_type,
            errors=tuple(errors),
        )

