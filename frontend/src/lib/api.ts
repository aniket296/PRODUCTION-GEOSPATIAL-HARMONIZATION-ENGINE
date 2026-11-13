import type {
  AutoResolveOptions,
  AutoResolveResponse,
  ExportFormat,
  HarmonizationAuditManifest,
  LayerExportPresignRequest,
  LayerExportPresignResponse,
  LayerStatusResponse,
  LayerUploadResponse,
  TopologyErrorFeatureCollection,
} from "@/types/gis";

const API_BASE = "/api/v1";

async function parseJsonOrThrow<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`.trim();
    try {
      const body = (await response.json()) as { detail?: string; message?: string };
      if (body?.detail) {
        detail = body.detail;
      } else if (body?.message) {
        detail = body.message;
      }
    } catch {
      // Fallback to HTTP status text when response is not JSON
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export interface UploadLayerParams {
  file: File;
  name?: string;
  declaredCrs?: string;
}

export async function uploadLayer({
  file,
  name,
  declaredCrs,
}: UploadLayerParams): Promise<LayerUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  if (name) {
    formData.append("name", name);
  }
  if (declaredCrs) {
    formData.append("declared_crs", declaredCrs);
  }

  const response = await fetch(`${API_BASE}/layers/upload`, {
    method: "POST",
    body: formData,
  });
  return parseJsonOrThrow<LayerUploadResponse>(response);
}

export async function getLayerStatus(layerId: string): Promise<LayerStatusResponse> {
  const response = await fetch(`${API_BASE}/layers/${encodeURIComponent(layerId)}/status`);
  return parseJsonOrThrow<LayerStatusResponse>(response);
}

export async function getTopologyErrors(
  layerId: string
): Promise<TopologyErrorFeatureCollection> {
  const response = await fetch(
    `${API_BASE}/layers/${encodeURIComponent(layerId)}/topology-errors`
  );
  return parseJsonOrThrow<TopologyErrorFeatureCollection>(response);
}

export async function autoResolveLayer(
  layerId: string,
  options: AutoResolveOptions = {}
): Promise<AutoResolveResponse> {
  const payload = {
    sliver_area_threshold_sqm: options.sliverAreaThresholdSqm,
    overlap_threshold_pct: options.overlapThresholdPct,
    gap_snap_tolerance_m: options.gapSnapToleranceM,
    grid_snap_precision_m: options.gridSnapPrecisionM,
  };

  const response = await fetch(
    `${API_BASE}/layers/${encodeURIComponent(layerId)}/auto-resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  return parseJsonOrThrow<AutoResolveResponse>(response);
}

export async function presignLayerExport(
  layerId: string,
  request: LayerExportPresignRequest
): Promise<LayerExportPresignResponse> {
  const response = await fetch(
    `${API_BASE}/layers/${encodeURIComponent(layerId)}/export/presign`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    }
  );
  return parseJsonOrThrow<LayerExportPresignResponse>(response);
}

export async function getExportManifest(
  layerId: string
): Promise<HarmonizationAuditManifest> {
  const response = await fetch(
    `${API_BASE}/layers/${encodeURIComponent(layerId)}/export/manifest`
  );
  return parseJsonOrThrow<HarmonizationAuditManifest>(response);
}

export function getDirectExportUrl(
  layerId: string,
  format: ExportFormat,
  srid = 4326
): string {
  return `${API_BASE}/layers/${encodeURIComponent(layerId)}/export?format=${encodeURIComponent(format)}&srid=${srid}`;
}

export function getFlatGeobufUrl(
  layerId: string,
  bbox?: [number, number, number, number]
): string {
  const base = `${API_BASE}/layers/${encodeURIComponent(layerId)}/flatgeobuf`;
  if (!bbox) {
    return base;
  }
  return `${base}?bbox=${bbox.join(",")}`;
}
