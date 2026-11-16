import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkbenchHeader } from "@/components/WorkbenchHeader";

describe("WorkbenchHeader", () => {
  it("disables export button when no layer is active and displays idle badge", () => {
    render(
      <WorkbenchHeader
        activeLayerId={null}
        layerStatus={null}
        onOpenExport={vi.fn()}
        onResetWorkbench={vi.fn()}
      />
    );

    expect(screen.getByText(/Geospatial Harmonization Engine/i)).toBeInTheDocument();
    expect(screen.getByText(/No Active Layer/i)).toBeInTheDocument();

    const exportBtn = screen.getByRole("button", { name: /export package/i });
    expect(exportBtn).toBeDisabled();
  });

  it("enables export button and triggers export modal and workbench reset callbacks", () => {
    const onOpenExport = vi.fn();
    const onResetWorkbench = vi.fn();

    render(
      <WorkbenchHeader
        activeLayerId="layer-14"
        layerStatus={{
          layer_id: "layer-14",
          name: "Ward 14 Master",
          status: "COMPLETED",
          progress_percent: 100,
          feature_count: 142,
          detected_crs: "EPSG:32643",
        }}
        onOpenExport={onOpenExport}
        onResetWorkbench={onResetWorkbench}
      />
    );

    expect(screen.getByText(/Ward 14 Master/i)).toBeInTheDocument();
    expect(screen.getByText(/142 features/i)).toBeInTheDocument();

    const exportBtn = screen.getByRole("button", { name: /export package/i });
    expect(exportBtn).not.toBeDisabled();
    fireEvent.click(exportBtn);
    expect(onOpenExport).toHaveBeenCalledOnce();

    const resetBtn = screen.getByRole("button", { name: /reset workbench/i });
    fireEvent.click(resetBtn);
    expect(onResetWorkbench).toHaveBeenCalledOnce();
  });
});
