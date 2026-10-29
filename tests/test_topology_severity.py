"""Tests for deterministic topology severity classification (catch-up Step 82/150).

Directive Section 15 (overlap):
- An overlap is ``AUTO_RESOLVABLE`` when the configured overlap threshold is satisfied (default ``<= 2%``).
- Overlaps above the threshold are ``CRITICAL_DISPUTE``.
- Overlap percentages are computed independently per parcel, so the threshold must be applied to BOTH: a 1% overlap
  on a large estate that is 40% of an adjacent small plot is a critical dispute.
- The threshold is centralized in configuration (``Settings.max_overlap_percent_threshold``).

Policy choices that the directive does not state explicitly (documented here so they are reviewable):
- A parcel that is already dispute-locked can never be auto-resolved: any error touching it is ``CRITICAL_DISPUTE``
  (matches ``HarmonizedParcel.lock_dispute`` / ``mark_auto_resolved``).
- ``SLIVER``, ``GAP``, ``DUPLICATE_VERTEX`` and ``DANGLING_NODE`` are mechanically healable with the configured
  tolerances (union, snap, grid normalization) and are ``AUTO_RESOLVABLE``.
- ``INVALID_GEOMETRY`` has no deterministic healing rule in the directive and is reported as ``WARNING``.
"""

from __future__ import annotations

import pytest

from backend.core.config import get_settings
from backend.models.topology import TopologicalError, TopologyErrorType, TopologySeverity
from backend.topology.severity import (
    DEFAULT_OVERLAP_THRESHOLD_PERCENT,
    classify_topology_severity,
)

OVERLAP = TopologyErrorType.OVERLAP


class TestOverlapClassification:
    """Overlap severity from the two independent percentages and the configured threshold."""

    def test_default_threshold_is_centralized_in_settings(self):
        assert DEFAULT_OVERLAP_THRESHOLD_PERCENT == 2.0
        assert DEFAULT_OVERLAP_THRESHOLD_PERCENT == get_settings().max_overlap_percent_threshold

    @pytest.mark.parametrize(
        "pct_a, pct_b",
        [(0.0, 0.0), (0.15, 1.9), (1.0, 1.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)],
    )
    def test_at_or_below_threshold_on_both_parcels_is_auto_resolvable(self, pct_a, pct_b):
        assert classify_topology_severity(OVERLAP, pct_a, pct_b) == TopologySeverity.AUTO_RESOLVABLE

    @pytest.mark.parametrize(
        "pct_a, pct_b",
        [(2.0001, 0.0), (0.0, 2.0001), (2.5, 2.5), (100.0, 100.0), (1.0, 40.0), (40.0, 1.0)],
    )
    def test_above_threshold_on_either_parcel_is_critical_dispute(self, pct_a, pct_b):
        assert classify_topology_severity(OVERLAP, pct_a, pct_b) == TopologySeverity.CRITICAL_DISPUTE

    def test_small_plot_encroachment_is_not_masked_by_large_neighbour(self):
        """15 m² on a 10,000 m² estate is 0.15% but 15% of a 100 m² plot -> critical."""
        assert classify_topology_severity(OVERLAP, 0.15, 15.0) == TopologySeverity.CRITICAL_DISPUTE

    def test_custom_threshold_overrides_default(self):
        assert classify_topology_severity(OVERLAP, 3.0, 3.0) == TopologySeverity.CRITICAL_DISPUTE
        assert (
            classify_topology_severity(OVERLAP, 3.0, 3.0, threshold_percent=5.0)
            == TopologySeverity.AUTO_RESOLVABLE
        )
        assert (
            classify_topology_severity(OVERLAP, 1.0, 1.0, threshold_percent=0.5)
            == TopologySeverity.CRITICAL_DISPUTE
        )

    def test_agrees_with_model_classifier(self):
        for pct_a, pct_b in [(0.0, 0.0), (2.0, 2.0), (2.1, 0.0), (0.0, 7.0), (1.0, 40.0)]:
            assert classify_topology_severity(OVERLAP, pct_a, pct_b) == TopologicalError.classify_overlap_severity(
                pct_a, pct_b, threshold=2.0
            )

    @pytest.mark.parametrize(
        "pct_a, pct_b",
        [(None, 1.0), (1.0, None), (None, None), (-0.1, 1.0), (1.0, 100.1), (float("nan"), 1.0), (1.0, float("inf"))],
    )
    def test_missing_or_invalid_percentages_raise_value_error(self, pct_a, pct_b):
        with pytest.raises(ValueError):
            classify_topology_severity(OVERLAP, pct_a, pct_b)

    @pytest.mark.parametrize("bad_threshold", [-0.1, 100.1, float("nan"), float("inf")])
    def test_invalid_threshold_raises_value_error(self, bad_threshold):
        with pytest.raises(ValueError, match="threshold_percent"):
            classify_topology_severity(OVERLAP, 1.0, 1.0, threshold_percent=bad_threshold)


class TestNonOverlapClassification:
    """Severity for the remaining error types and dispute-lock handling."""

    @pytest.mark.parametrize(
        "error_type",
        [
            TopologyErrorType.SLIVER,
            TopologyErrorType.GAP,
            TopologyErrorType.DUPLICATE_VERTEX,
            TopologyErrorType.DANGLING_NODE,
        ],
    )
    def test_mechanically_healable_types_are_auto_resolvable(self, error_type):
        assert classify_topology_severity(error_type) == TopologySeverity.AUTO_RESOLVABLE

    def test_invalid_geometry_is_warning(self):
        assert classify_topology_severity(TopologyErrorType.INVALID_GEOMETRY) == TopologySeverity.WARNING

    @pytest.mark.parametrize("error_type", list(TopologyErrorType))
    def test_dispute_locked_parcel_is_always_critical(self, error_type):
        severity = classify_topology_severity(
            error_type,
            0.0 if error_type == TopologyErrorType.OVERLAP else None,
            0.0 if error_type == TopologyErrorType.OVERLAP else None,
            involves_dispute_locked_parcel=True,
        )
        assert severity == TopologySeverity.CRITICAL_DISPUTE

    def test_percentages_are_ignored_for_non_overlap_types(self):
        assert (
            classify_topology_severity(TopologyErrorType.GAP, overlap_percentage_primary=99.0)
            == TopologySeverity.AUTO_RESOLVABLE
        )

    def test_accepts_string_error_type_and_rejects_unknown(self):
        assert classify_topology_severity("SLIVER") == TopologySeverity.AUTO_RESOLVABLE
        assert classify_topology_severity("OVERLAP", 1.0, 1.0) == TopologySeverity.AUTO_RESOLVABLE
        with pytest.raises(ValueError, match="error_type"):
            classify_topology_severity("NOT_A_TYPE")

    def test_classification_is_deterministic(self):
        results = {classify_topology_severity(OVERLAP, 2.0, 1.99) for _ in range(50)}
        assert results == {TopologySeverity.AUTO_RESOLVABLE}

    def test_returned_values_satisfy_model_severity_constraint(self):
        allowed = {s.value for s in TopologySeverity}
        for error_type in TopologyErrorType:
            pct = 1.0 if error_type == TopologyErrorType.OVERLAP else None
            assert classify_topology_severity(error_type, pct, pct).value in allowed
