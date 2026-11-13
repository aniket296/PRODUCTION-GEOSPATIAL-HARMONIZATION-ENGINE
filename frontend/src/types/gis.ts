export type LayerLifecycleStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";

export type TopologyErrorType =
  | "OVERLAP"
  | "SLIVER"
  | "GAP"
  | "DANGLING_NODE"
  | "DUPLICATE_VERTEX"
  | "INVALID_GEOMETRY";

export type TopologySeverity = "AUTO_RESOLVABLE" | "CRITICAL_DISPUTE" | "WARNING" | "INFO";

export type ExportFormat = "geojson" | "gpkg" | "fgb";

export interface LayerUploadResponse {
  layer_id: string;
  job_id: string;
  status: LayerLifecycleStatus | string;
  message?: string;
  filename: string;
  file_size_bytes: number;
  detected_format?: string | null;
  declared_crs?: string | null;
  target_crs: string;
  created_at: string;
}

export interface LayerStatusResponse {
  layer_id: string;
  name?: string | null;
  filename?: string | null;
  file_format?: string | null;
  status: LayerLifecycleStatus | string;
  stage?: string | null;
  progress_percent: number;
  feature_count: number;
  total_area_sqm?: number | null;
  is_raster?: boolean;
  detected_crs?: string | null;
  bounds?: [number, number, number, number] | null;
  error_message?: string | null;
  job_id?: string | null;
  created_at?: string;
  updated_at?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface TopologyErrorProperties {
  error_id: string;
  layer_id: string;
  error_type: TopologyErrorType | string;
  severity: TopologySeverity | string;
  affected_area_sqm?: number | null;
  is_resolved: boolean;
  primary_parcel_id?: string | null;
  secondary_parcel_id?: string | null;
  description?: string | null;
}

export interface TopologyErrorFeature {
  type: "Feature";
  id?: string;
  geometry: GeoJSONGeometry | null;
  properties: TopologyErrorProperties;
}

export interface TopologyErrorFeatureCollection {
  type: "FeatureCollection";
  features: TopologyErrorFeature[];
}

export interface GeoJSONGeometry {
  type: string;
  coordinates: unknown;
}

export interface AutoResolveOptions {
  sliverAreaThresholdSqm?: number;
  overlapThresholdPct?: number;
  gapSnapToleranceM?: number;
  gridSnapPrecisionM?: number;
}

export interface AutoResolveResponse {
  layer_id: string;
  resolved_count: number;
  skipped_disputed_count: number;
  slivers_merged?: number;
  overlaps_trimmed?: number;
  gaps_closed?: number;
  vertices_snapped?: number;
  verification_passed?: boolean;
  message?: string;
}

export interface LayerExportPresignRequest {
  format: ExportFormat;
  srid?: number;
  include_merged_slivers?: boolean;
  expires_seconds?: number;
  force_rebuild?: boolean;
}

export interface LayerExportPresignResponse {
  layer_id: string;
  format: ExportFormat | string;
  bucket: string;
  object_name: string;
  download_url: string;
  content_type: string;
  byte_size: number;
  sha256: string;
  feature_count: number;
  expires_seconds: number;
  cached: boolean;
}

export interface HarmonizationAuditManifest {
  schema_version: string;
  layer: {
    layer_id: string;
    name: string | null;
    filename: string | null;
    file_format: string | null;
    source_crs: string | null;
    target_crs: string | null;
    status: string;
  };
  parcels: {
    total_parcels: number;
    active_parcels: number;
    merged_slivers: number;
    disputed_parcels: number;
    total_active_area_sqm: number;
  };
  topology: {
    total_errors: number;
    resolved_errors: number;
    unresolved_errors: number;
    by_type: Record<string, number>;
    by_severity: Record<string, number>;
  };
  artifacts: Record<string, { sha256: string }>;
  manifest_sha256: string;
}
