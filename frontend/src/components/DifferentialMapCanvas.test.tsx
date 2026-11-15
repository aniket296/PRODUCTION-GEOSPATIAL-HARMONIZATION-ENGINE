import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DifferentialMapCanvas } from "@/components/DifferentialMapCanvas";
import type { ParcelGeoJSONFeatureCollection } from "@/lib/flatgeobufClient";
import type { TopologyErrorFeature } from "@/types/gis";

const mockParcels: ParcelGeoJSONFeatureCollection = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      id: "p-1",
      geometry: {
        type: "MultiPolygon",
        coordinates: [[[[77.2, 28.6], [77.21, 28.6], [77.21, 28.61], [77.2, 28.61], [77.2, 28.6]]]],
      },
      properties: {
        parcel_identifier: "P-101",
        metric_area_sqm: 1000,
        is_disputed: false,
      },
    },
  ],
};

const mockErrors: TopologyErrorFeature[] = [
  {
    type: "Feature",
    id: "err-1",
    geometry: { type: "Polygon", coordinates: [[[77.2, 28.6], [77.205, 28.6], [77.205, 28.605], [77.2, 28.6]]] },
    properties: {
      error_id: "err-1",
      layer_id: "layer-1",
      error_type: "OVERLAP",
      severity: "CRITICAL_DISPUTE",
      affected_area_sqm: 18.4,
      is_resolved: false,
    },
  },
];

describe("DifferentialMapCanvas", () => {
  it("renders swipe slider, view mode toggles, and updates swipe position", () => {
    const onSwipeChange = vi.fn();
    const onViewModeChange = vi.fn();

    render(
      <DifferentialMapCanvas
        parcels={mockParcels}
        topologyErrors={mockErrors}
        viewMode="swipe"
        swipePosition={50}
        selectedErrorId={null}
        selectedParcelId={null}
        onSwipeChange={onSwipeChange}
        onViewModeChange={onViewModeChange}
        onSelectParcel={vi.fn()}
        onSelectError={vi.fn()}
      />
    );

    const slider = screen.getByLabelText(/swipe divider position/i);
    expect(slider).toHaveValue("50");

    fireEvent.change(slider, { target: { value: "72" } });
    expect(onSwipeChange).toHaveBeenCalledWith(72);

    const overlayBtn = screen.getByRole("button", { name: /overlay/i });
    fireEvent.click(overlayBtn);
    expect(onViewModeChange).toHaveBeenCalledWith("overlay");
  });

  it("displays parcel and topology defect counts in the HUD legend and selects a parcel", () => {
    const onSelectParcel = vi.fn();

    render(
      <DifferentialMapCanvas
        parcels={mockParcels}
        topologyErrors={mockErrors}
        viewMode="overlay"
        swipePosition={50}
        selectedErrorId={null}
        selectedParcelId={null}
        onSwipeChange={vi.fn()}
        onViewModeChange={vi.fn()}
        onSelectParcel={onSelectParcel}
        onSelectError={vi.fn()}
      />
    );

    expect(screen.getByText(/1 Harmonized Parcels/i)).toBeInTheDocument();
    expect(screen.getByText(/1 Active Defects/i)).toBeInTheDocument();

    const parcelChip = screen.getByRole("button", { name: /P-101/i });
    fireEvent.click(parcelChip);
    expect(onSelectParcel).toHaveBeenCalledWith("P-101");
  });
});
