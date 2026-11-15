import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TopologyInspectorPanel } from "@/components/TopologyInspectorPanel";
import type { TopologyErrorFeature } from "@/types/gis";

const mockErrors: TopologyErrorFeature[] = [
  {
    type: "Feature",
    id: "err-1",
    geometry: { type: "Polygon", coordinates: [] },
    properties: {
      error_id: "err-1",
      layer_id: "layer-1",
      error_type: "SLIVER",
      severity: "AUTO_RESOLVABLE",
      affected_area_sqm: 1.25,
      is_resolved: false,
      primary_parcel_id: "P-101",
      description: "Micro sliver along eastern boundary",
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
      affected_area_sqm: 42.8,
      is_resolved: false,
      primary_parcel_id: "P-102",
      secondary_parcel_id: "P-103",
      description: "Critical boundary dispute overlap",
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
      affected_area_sqm: 3.5,
      is_resolved: true,
      description: "Resolved gap between survey monuments",
    },
  },
];

describe("TopologyInspectorPanel", () => {
  it("renders error list with count badges and filter dropdowns", () => {
    const onSelectError = vi.fn();
    const onSeverityChange = vi.fn();
    const onTypeChange = vi.fn();
    const onShowResolvedChange = vi.fn();

    render(
      <TopologyInspectorPanel
        errors={mockErrors}
        selectedErrorId={null}
        severityFilter="ALL"
        typeFilter="ALL"
        showResolvedErrors={false}
        onSelectError={onSelectError}
        onSeverityChange={onSeverityChange}
        onTypeChange={onTypeChange}
        onShowResolvedChange={onShowResolvedChange}
      />
    );

    expect(screen.getByText(/Topology Defects/i)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument(); // total badge
    expect(screen.getByText(/SLIVER/i)).toBeInTheDocument();
    expect(screen.getByText(/OVERLAP/i)).toBeInTheDocument();
  });

  it("triggers selection callback when an error card is clicked", () => {
    const onSelectError = vi.fn();

    render(
      <TopologyInspectorPanel
        errors={mockErrors}
        selectedErrorId="err-2"
        severityFilter="ALL"
        typeFilter="ALL"
        showResolvedErrors={false}
        onSelectError={onSelectError}
        onSeverityChange={vi.fn()}
        onTypeChange={vi.fn()}
        onShowResolvedChange={vi.fn()}
      />
    );

    const firstCard = screen.getByText(/Micro sliver along eastern boundary/i);
    fireEvent.click(firstCard);

    expect(onSelectError).toHaveBeenCalledWith("err-1");
  });

  it("displays empty state placeholder when no errors are present", () => {
    render(
      <TopologyInspectorPanel
        errors={[]}
        selectedErrorId={null}
        severityFilter="ALL"
        typeFilter="ALL"
        showResolvedErrors={false}
        onSelectError={vi.fn()}
        onSeverityChange={vi.fn()}
        onTypeChange={vi.fn()}
        onShowResolvedChange={vi.fn()}
      />
    );

    expect(screen.getByText(/No topological defects detected/i)).toBeInTheDocument();
  });
});
