"""Deterministic topology severity classifier.

Directive Section 15: an overlap is ``AUTO_RESOLVABLE`` when the configured overlap threshold is satisfied (default
``<= 2%``) and ``CRITICAL_DISPUTE`` above it. The two overlap percentages are computed independently per parcel, so
the threshold is applied to both and the larger one decides.

Policy for cases the directive does not spell out:

* A dispute-locked parcel can never be auto-resolved: any error involving it is ``CRITICAL_DISPUTE``.
* ``SLIVER``, ``GAP``, ``DUPLICATE_VERTEX`` and ``DANGLING_NODE`` are healable with the configured tolerances and are
  ``AUTO_RESOLVABLE``.
* ``INVALID_GEOMETRY`` has no deterministic healing rule and is reported as ``WARNING``.

The overlap threshold is read from ``Settings.max_overlap_percent_threshold`` unless explicitly overridden.
"""

from __future__ import annotations

import math

from backend.core.config import get_settings
from backend.models.topology import TopologyErrorType, TopologySeverity

#: Default overlap threshold (percent), resolved from configuration at import time for reference.
DEFAULT_OVERLAP_THRESHOLD_PERCENT: float = get_settings().max_overlap_percent_threshold

_AUTO_RESOLVABLE_TYPES = frozenset(
    {
        TopologyErrorType.SLIVER,
        TopologyErrorType.GAP,
        TopologyErrorType.DUPLICATE_VERTEX,
        TopologyErrorType.DANGLING_NODE,
    }
)


def _coerce_error_type(error_type: TopologyErrorType | str) -> TopologyErrorType:
    if isinstance(error_type, TopologyErrorType):
        return error_type
    try:
        return TopologyErrorType(error_type)
    except ValueError as exc:
        raise ValueError(f"Unknown error_type: {error_type!r}") from exc


def _validate_threshold(threshold_percent: float) -> None:
    if not math.isfinite(threshold_percent) or not 0.0 <= threshold_percent <= 100.0:
        raise ValueError("threshold_percent must be within [0, 100] and finite")


def _validate_percentage(name: str, value: float | None) -> float:
    if value is None:
        raise ValueError(f"{name} is required for OVERLAP classification")
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError(f"{name} must be within [0, 100] and finite")
    return float(value)


def classify_topology_severity(
    error_type: TopologyErrorType | str,
    overlap_percentage_primary: float | None = None,
    overlap_percentage_secondary: float | None = None,
    *,
    threshold_percent: float | None = None,
    involves_dispute_locked_parcel: bool = False,
) -> TopologySeverity:
    """Classify a topological error into a deterministic `TopologySeverity`.

    Args:
        error_type: Detected anomaly type (enum member or its string value).
        overlap_percentage_primary: Overlap as a percentage of the primary parcel (required for ``OVERLAP``).
        overlap_percentage_secondary: Overlap as a percentage of the secondary parcel (required for ``OVERLAP``).
        threshold_percent: Overlap threshold; defaults to ``Settings.max_overlap_percent_threshold``.
        involves_dispute_locked_parcel: True if either parcel is already dispute-locked.

    Raises:
        ValueError: On an unknown ``error_type``, an invalid threshold, or missing/invalid overlap percentages.
    """
    kind = _coerce_error_type(error_type)
    threshold = (
        get_settings().max_overlap_percent_threshold if threshold_percent is None else float(threshold_percent)
    )
    _validate_threshold(threshold)

    if involves_dispute_locked_parcel:
        return TopologySeverity.CRITICAL_DISPUTE

    if kind is TopologyErrorType.OVERLAP:
        pct_a = _validate_percentage("overlap_percentage_primary", overlap_percentage_primary)
        pct_b = _validate_percentage("overlap_percentage_secondary", overlap_percentage_secondary)
        if max(pct_a, pct_b) > threshold:
            return TopologySeverity.CRITICAL_DISPUTE
        return TopologySeverity.AUTO_RESOLVABLE

    if kind in _AUTO_RESOLVABLE_TYPES:
        return TopologySeverity.AUTO_RESOLVABLE

    return TopologySeverity.WARNING

