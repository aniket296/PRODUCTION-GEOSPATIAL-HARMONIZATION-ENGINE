import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  autoResolveLayer,
  getDirectExportUrl,
  getExportManifest,
  getLayerStatus,
  getTopologyErrors,
  presignLayerExport,
  uploadLayer,
} from "@/lib/api";

describe("GIS API client", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("uploads a layer with FormData and returns LayerUploadResponse", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        layer_id: "11111111-1111-1111-1111-111111111111",
        job_id: "job-1",
        status: "PENDING",
        filename: "ward14.geojson",
        file_size_bytes: 128,
        target_crs: "EPSG:4326",
        created_at: "2026-11-13T00:00:00Z",
      }),
    });
    vi.stubGlobal("fetch", mockFetch);

    const file = new File(["{}"], "ward14.geojson", { type: "application/geo+json" });
    const res = await uploadLayer({ file, name: "Ward 14", declaredCrs: "EPSG:32643" });

    expect(res.layer_id).toBe("11111111-1111-1111-1111-111111111111");
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/v1/layers/upload",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) })
    );
  });

  it("fetches layer status, topology errors, auto-resolve, presign, and manifest", async () => {
    const layerId = "22222222-2222-2222-2222-222222222222";
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ layer_id: layerId, status: "COMPLETED", progress_percent: 100, feature_count: 12 }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ type: "FeatureCollection", features: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ layer_id: layerId, resolved_count: 4, skipped_disputed_count: 1 }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          layer_id: layerId,
          format: "gpkg",
          download_url: "https://minio.local/presigned",
          byte_size: 2048,
          sha256: "a".repeat(64),
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ schema_version: "1.0.0", manifest_sha256: "b".repeat(64) }),
      });

    vi.stubGlobal("fetch", mockFetch);

    const status = await getLayerStatus(layerId);
    expect(status.status).toBe("COMPLETED");

    const errors = await getTopologyErrors(layerId);
    expect(errors.type).toBe("FeatureCollection");

    const resolved = await autoResolveLayer(layerId, { sliverAreaThresholdSqm: 2.0 });
    expect(resolved.resolved_count).toBe(4);

    const presigned = await presignLayerExport(layerId, { format: "gpkg", srid: 32643 });
    expect(presigned.download_url).toBe("https://minio.local/presigned");

    const manifest = await getExportManifest(layerId);
    expect(manifest.schema_version).toBe("1.0.0");
  });

  it("throws a descriptive error on non-OK HTTP responses and builds direct export URLs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        statusText: "Not Found",
        json: async () => ({ detail: "Layer not found" }),
      })
    );

    await expect(getLayerStatus("missing-id")).rejects.toThrow("Layer not found");
    expect(getDirectExportUrl("abc", "geojson")).toBe("/api/v1/layers/abc/export?format=geojson&srid=4326");
    expect(getDirectExportUrl("abc", "gpkg", 32643)).toBe("/api/v1/layers/abc/export?format=gpkg&srid=32643");
  });
});
