import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import * as api from "@/lib/api";
import { useGisStore } from "@/store/gisStore";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    uploadLayer: vi.fn(),
    getLayerStatus: vi.fn(),
    getTopologyErrors: vi.fn(),
    autoResolveLayer: vi.fn(),
    presignLayerExport: vi.fn(),
    getExportManifest: vi.fn(),
  };
});

describe("Integrated App GIS Workbench", () => {
  beforeEach(() => {
    useGisStore.getState().resetWorkbench();
    vi.clearAllMocks();
  });

  it("executes upload -> status/topology load -> auto-heal -> export modal flow", async () => {
    const layerId = "layer-14-uuid";
    vi.mocked(api.uploadLayer).mockResolvedValue({
      layer_id: layerId,
      job_id: "job-14",
      status: "COMPLETED",
      filename: "ward14.geojson",
      file_size_bytes: 512,
      target_crs: "EPSG:4326",
      created_at: "2026-11-16T00:00:00Z",
    });

    vi.mocked(api.getLayerStatus).mockResolvedValue({
      layer_id: layerId,
      name: "Ward 14 Master",
      status: "COMPLETED",
      progress_percent: 100,
      feature_count: 24,
      detected_crs: "EPSG:32643",
    });

    vi.mocked(api.getTopologyErrors)
      .mockResolvedValueOnce({
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            id: "err-1",
            geometry: { type: "Polygon", coordinates: [] },
            properties: {
              error_id: "err-1",
              layer_id: layerId,
              error_type: "SLIVER",
              severity: "AUTO_RESOLVABLE",
              affected_area_sqm: 1.2,
              is_resolved: false,
              description: "Sliver defect on parcel P-1",
            },
          },
        ],
      })
      .mockResolvedValueOnce({
        type: "FeatureCollection",
        features: [],
      });

    vi.mocked(api.autoResolveLayer).mockResolvedValue({
      layer_id: layerId,
      resolved_count: 4,
      skipped_disputed_count: 1,
      slivers_merged: 3,
      overlaps_trimmed: 1,
      gaps_closed: 0,
      verification_passed: true,
    });

    render(<App />);

    // 1. Upload file
    const fileInput = screen.getByLabelText(/cadastral file/i);
    const sampleFile = new File(["{}"], "ward14.geojson", { type: "application/geo+json" });
    fireEvent.change(fileInput, { target: { files: [sampleFile] } });

    const ingestBtn = screen.getByRole("button", { name: /ingest survey layer/i });
    fireEvent.click(ingestBtn);

    await waitFor(() => {
      expect(screen.getByText(/Ward 14 Master/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Sliver defect on parcel P-1/i)).toBeInTheDocument();

    // 2. Trigger Auto-Heal
    const healBtn = screen.getByRole("button", { name: /auto-heal layer/i });
    fireEvent.click(healBtn);

    await waitFor(() => {
      expect(screen.getByText(/4 defects resolved/i)).toBeInTheDocument();
    });

    // 3. Open Export Modal
    const exportBtn = screen.getByRole("button", { name: /export package/i });
    fireEvent.click(exportBtn);

    expect(screen.getByRole("dialog", { name: /Export Harmonized Cadastre/i })).toBeInTheDocument();
  });
});
