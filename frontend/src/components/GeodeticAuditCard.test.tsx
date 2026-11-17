import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GeodeticAuditCard } from "@/components/GeodeticAuditCard";

describe("GeodeticAuditCard", () => {
  it("renders idle calibration notice when metadata is null", () => {
    render(<GeodeticAuditCard sourceCrs={null} targetCrs="EPSG:4326" metadata={null} />);
    expect(screen.getByText(/Awaiting survey geodetic calibration/i)).toBeInTheDocument();
  });

  it("renders Helmert 7-parameter RMSE, GCP count, datum path, and TPS status", () => {
    render(
      <GeodeticAuditCard
        sourceCrs="EPSG:7755"
        targetCrs="EPSG:4326"
        metadata={{
          rmse_meters: 0.084,
          gcp_count: 12,
          scale_ppm: 2.35,
          rotation_arcsec: 0.42,
          thin_plate_spline_applied: true,
        }}
      />
    );

    expect(screen.getByText(/EPSG:7755 → EPSG:4326/i)).toBeInTheDocument();
    expect(screen.getByText(/0\.084 m/i)).toBeInTheDocument();
    expect(screen.getByText(/12 GCPs/i)).toBeInTheDocument();
    expect(screen.getByText(/TPS Non-Linear Warp Active/i)).toBeInTheDocument();
  });
});
