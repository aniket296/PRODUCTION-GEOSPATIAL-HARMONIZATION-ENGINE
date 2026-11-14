import { beforeEach, describe, expect, it } from "vitest";
import { useGisStore } from "@/store/gisStore";
import type { TopologyErrorFeature } from "@/types/gis";

const sampleErrors: TopologyErrorFeature[] = [
  {
    type: "Feature",
    id: "err-1",
    geometry: { type: "Polygon", coordinates: [] },
    properties: {
      error_id: "err-1",
      layer_id: "layer-1",
      error_type: "SLIVER",
      severity: "AUTO_RESOLVABLE",
      affected_area_sqm: 1.4,
      is_resolved: false,
    },
  },
  {
    type: "Feature",
    id: "err-2",
    geometry: { type: "Polygon", coordinates: [] },
    properties: {
      error_id: "err-2",
      layer_id: "layer-1",
      error_type: "OVERLAP",
      severity: "CRITICAL_DISPUTE",
      affected_area_sqm: 45.2,
      is_resolved: false,
    },
  },
  {
    type: "Feature",
    id: "err-3",
    geometry: { type: "Polygon", coordinates: [] },
    properties: {
      error_id: "err-3",
      layer_id: "layer-1",
      error_type: "GAP",
      severity: "WARNING",
      affected_area_sqm: 3.1,
      is_resolved: true,
    },
  },
];

describe("useGisStore workbench state", () => {
  beforeEach(() => {
    useGisStore.getState().resetWorkbench();
  });

  it("initializes with canonical default differential swipe workbench state", () => {
    const state = useGisStore.getState();
    expect(state.activeLayerId).toBeNull();
    expect(state.viewMode).toBe("swipe");
    expect(state.swipePosition).toBe(50);
    expect(state.severityFilter).toBe("ALL");
    expect(state.typeFilter).toBe("ALL");
    expect(state.showResolvedErrors).toBe(false);
    expect(state.selectedErrorId).toBeNull();
  });

  it("clamps swipePosition strictly into [0, 100] and switches viewMode", () => {
    const store = useGisStore.getState();
    store.setSwipePosition(-15);
    expect(useGisStore.getState().swipePosition).toBe(0);

    store.setSwipePosition(140);
    expect(useGisStore.getState().swipePosition).toBe(100);

    store.setSwipePosition(68);
    expect(useGisStore.getState().swipePosition).toBe(68);

    store.setViewMode("overlay");
    expect(useGisStore.getState().viewMode).toBe("overlay");
  });

  it("filters topology errors by severity, error_type, and resolution state", () => {
    const store = useGisStore.getState();
    store.setActiveLayer("layer-1");
    store.setTopologyErrors(sampleErrors);

    // By default showResolvedErrors=false -> 2 unresolved errors
    expect(useGisStore.getState().getFilteredTopologyErrors()).toHaveLength(2);

    // Include resolved -> all 3 errors
    store.setShowResolvedErrors(true);
    expect(useGisStore.getState().getFilteredTopologyErrors()).toHaveLength(3);

    // Filter by CRITICAL_DISPUTE
    store.setSeverityFilter("CRITICAL_DISPUTE");
    const critical = useGisStore.getState().getFilteredTopologyErrors();
    expect(critical).toHaveLength(1);
    expect(critical[0].properties.error_id).toBe("err-2");

    // Reset severity, filter by SLIVER
    store.setSeverityFilter("ALL");
    store.setTypeFilter("SLIVER");
    const slivers = useGisStore.getState().getFilteredTopologyErrors();
    expect(slivers).toHaveLength(1);
    expect(slivers[0].properties.error_id).toBe("err-1");
  });
});
