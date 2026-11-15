import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LayerUploadPanel } from "@/components/LayerUploadPanel";

describe("LayerUploadPanel", () => {
  it("disables upload button until a file is chosen and submits file, name, and CRS", async () => {
    const onUpload = vi.fn().mockResolvedValue(undefined);
    render(<LayerUploadPanel onUpload={onUpload} layerStatus={null} isUploading={false} />);

    const submitBtn = screen.getByRole("button", { name: /ingest survey layer/i });
    expect(submitBtn).toBeDisabled();

    const nameInput = screen.getByLabelText(/layer name/i);
    fireEvent.change(nameInput, { target: { value: "Ward 14 Survey" } });

    const crsSelect = screen.getByLabelText(/declared crs/i);
    fireEvent.change(crsSelect, { target: { value: "EPSG:32643" } });

    const fileInput = screen.getByLabelText(/cadastral file/i);
    const sampleFile = new File(["{}"], "ward14.geojson", { type: "application/geo+json" });
    fireEvent.change(fileInput, { target: { files: [sampleFile] } });

    expect(submitBtn).not.toBeDisabled();
    fireEvent.click(submitBtn);

    expect(onUpload).toHaveBeenCalledWith({
      file: sampleFile,
      name: "Ward 14 Survey",
      declaredCrs: "EPSG:32643",
    });
  });

  it("renders active layer lifecycle status, progress bar, and feature count", () => {
    render(
      <LayerUploadPanel
        onUpload={vi.fn()}
        isUploading={false}
        layerStatus={{
          layer_id: "layer-99",
          name: "Ward 14 Survey",
          status: "COMPLETED",
          progress_percent: 100,
          feature_count: 248,
          detected_crs: "EPSG:32643",
          total_area_sqm: 125400.5,
        }}
      />
    );

    expect(screen.getByText(/COMPLETED/i)).toBeInTheDocument();
    expect(screen.getByText(/248/)).toBeInTheDocument();
    expect(screen.getByText(/100%/)).toBeInTheDocument();
  });
});
