import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ExportModal } from "@/components/ExportModal";
import type { HarmonizationAuditManifest, LayerExportPresignResponse } from "@/types/gis";

const mockManifest: HarmonizationAuditManifest = {
  schema_version: "1.0.0",
  layer: {
    layer_id: "layer-1",
    name: "Ward 14 Cadastral",
    filename: "ward14.geojson",
    file_format: "GEOJSON",
    source_crs: "EPSG:32643",
    target_crs: "EPSG:4326",
    status: "COMPLETED",
  },
  parcels: {
    total_parcels: 10,
    active_parcels: 9,
    merged_slivers: 1,
    disputed_parcels: 1,
    total_active_area_sqm: 14500.5,
  },
  topology: {
    total_errors: 2,
    resolved_errors: 1,
    unresolved_errors: 1,
    by_type: { SLIVER: 1, OVERLAP: 1 },
    by_severity: { AUTO_RESOLVABLE: 1, CRITICAL_DISPUTE: 1 },
  },
  artifacts: {
    gpkg: { sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" },
  },
  manifest_sha256: "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
};

const mockStaged: LayerExportPresignResponse = {
  layer_id: "layer-1",
  format: "gpkg",
  bucket: "cadastral-harmonized",
  object_name: "exports/layer-1/ward_14.gpkg",
  download_url: "https://minio.local/presigned-download",
  content_type: "application/geopackage+sqlite3",
  byte_size: 4096,
  sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  feature_count: 9,
  expires_seconds: 3600,
  cached: false,
};

describe("ExportModal", () => {
  it("renders export formats, triggers presign staging, and displays download link", () => {
    const onPresign = vi.fn().mockResolvedValue(undefined);
    const onClose = vi.fn();

    render(
      <ExportModal
        isOpen={true}
        onClose={onClose}
        onPresign={onPresign}
        stagedExport={null}
        auditManifest={null}
        isStaging={false}
      />
    );

    expect(screen.getByText(/Export Harmonized Cadastre/i)).toBeInTheDocument();
    const gpkgRadio = screen.getByLabelText(/GeoPackage/i);
    fireEvent.click(gpkgRadio);

    const generateBtn = screen.getByRole("button", { name: /generate secure download/i });
    fireEvent.click(generateBtn);

    expect(onPresign).toHaveBeenCalledWith({
      format: "gpkg",
      srid: 4326,
      include_merged_slivers: false,
    });
  });

  it("displays presigned MinIO URL, SHA-256 checksum, and audit manifest summary", () => {
    render(
      <ExportModal
        isOpen={true}
        onClose={vi.fn()}
        onPresign={vi.fn()}
        stagedExport={mockStaged}
        auditManifest={mockManifest}
        isStaging={false}
      />
    );

    const downloadLink = screen.getByRole("link", { name: /download package/i });
    expect(downloadLink).toHaveAttribute("href", "https://minio.local/presigned-download");

    expect(screen.getByText(/e3b0c44298fc1c149/i)).toBeInTheDocument();
    expect(screen.getByText(/2cf24dba5fb0a30e/i)).toBeInTheDocument();
  });

  it("invokes onClose when the Escape key is pressed while open", () => {
    const onClose = vi.fn();

    render(
      <ExportModal
        isOpen={true}
        onClose={onClose}
        onPresign={vi.fn()}
        stagedExport={null}
        auditManifest={null}
        isStaging={false}
      />
    );

    fireEvent.keyDown(window, { key: "Escape", code: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
