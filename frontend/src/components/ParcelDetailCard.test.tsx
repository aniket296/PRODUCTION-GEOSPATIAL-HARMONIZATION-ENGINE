import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ParcelDetailCard } from "@/components/ParcelDetailCard";
import type { ParcelGeoJSONFeature } from "@/lib/flatgeobufClient";

const disputedParcel: ParcelGeoJSONFeature = {
  type: "Feature",
  id: "p-102",
  geometry: { type: "MultiPolygon", coordinates: [] },
  properties: {
    parcel_identifier: "P-14-102",
    metric_area_sqm: 1425.75,
    metric_perimeter_m: 158.4,
    polsby_popper_compactness: 0.714,
    is_disputed: true,
    dispute_locked: true,
    resolution_status: "DISPUTE_LOCKED",
    attributes: { owner: "Municipal Corp", khasra: "44/2" },
  },
};

describe("ParcelDetailCard", () => {
  it("renders empty selection prompt when parcel is null", () => {
    render(<ParcelDetailCard parcel={null} onClear={vi.fn()} />);
    expect(
      screen.getByText(/Select a parcel on the canvas to inspect geodetic metrics/i)
    ).toBeInTheDocument();
  });

  it("displays metric area, compactness, dispute lock badge, and custom attributes", () => {
    const onClear = vi.fn();
    render(<ParcelDetailCard parcel={disputedParcel} onClear={onClear} />);

    expect(screen.getByText("P-14-102")).toBeInTheDocument();
    expect(screen.getByText(/1425\.75 m²/i)).toBeInTheDocument();
    expect(screen.getByText(/0\.714/)).toBeInTheDocument();
    expect(screen.getByText(/DISPUTE LOCKED/i)).toBeInTheDocument();
    expect(screen.getByText(/Municipal Corp/i)).toBeInTheDocument();

    const clearBtn = screen.getByRole("button", { name: /clear selection/i });
    fireEvent.click(clearBtn);
    expect(onClear).toHaveBeenCalledOnce();
  });
});
