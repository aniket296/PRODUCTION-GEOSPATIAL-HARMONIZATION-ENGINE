import { useState } from "react";
import type {
  ExportFormat,
  HarmonizationAuditManifest,
  LayerExportPresignRequest,
  LayerExportPresignResponse,
} from "@/types/gis";

export interface ExportModalProps {
  isOpen: boolean;
  onClose: () => void;
  onPresign: (request: LayerExportPresignRequest) => Promise<void> | void;
  stagedExport: LayerExportPresignResponse | null;
  auditManifest: HarmonizationAuditManifest | null;
  isStaging: boolean;
}

const FORMAT_OPTIONS: { value: ExportFormat; label: string; desc: string }[] = [
  {
    value: "gpkg",
    label: "OGC GeoPackage (.gpkg)",
    desc: "SQLite container with spatial R-tree and full attribute retention",
  },
  {
    value: "geojson",
    label: "RFC 7946 GeoJSON (.geojson)",
    desc: "Canonical WGS 84 JSON with CCW exterior / CW interior winding",
  },
  {
    value: "fgb",
    label: "FlatGeobuf (.fgb)",
    desc: "Cloud-native Hilbert packed R-tree binary format",
  },
];

export function ExportModal({
  isOpen,
  onClose,
  onPresign,
  stagedExport,
  auditManifest,
  isStaging,
}: ExportModalProps) {
  const [format, setFormat] = useState<ExportFormat>("gpkg");
  const [srid, setSrid] = useState<number>(4326);
  const [includeMergedSlivers, setIncludeMergedSlivers] = useState<boolean>(false);

  if (!isOpen) {
    return null;
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Export Harmonized Cadastre"
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm"
    >
      <div className="w-full max-w-lg rounded-2xl border border-slate-800 bg-slate-900 p-6 text-slate-100 shadow-2xl">
        <div className="flex items-center justify-between border-b border-slate-800 pb-3">
          <div>
            <h2 className="text-base font-semibold text-white">
              Export Harmonized Cadastre
            </h2>
            <p className="text-xs text-slate-400">
              Stage cryptographic GIS package in MinIO and issue presigned URL
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-white"
          >
            Close
          </button>
        </div>

        <div className="mt-4 flex flex-col gap-3">
          <fieldset className="flex flex-col gap-2">
            <legend className="text-xs font-medium text-slate-300 mb-1">
              Output Format
            </legend>
            {FORMAT_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className={`flex cursor-pointer items-start gap-2.5 rounded-lg border p-2.5 text-xs transition-colors ${
                  format === opt.value
                    ? "border-indigo-500 bg-indigo-950/30"
                    : "border-slate-800 bg-slate-950/50 hover:border-slate-700"
                }`}
              >
                <input
                  type="radio"
                  name="export-format"
                  value={opt.value}
                  checked={format === opt.value}
                  onChange={() => setFormat(opt.value)}
                  className="mt-0.5"
                />
                <div>
                  <div className="font-medium text-slate-100">{opt.label}</div>
                  <div className="text-[11px] text-slate-400">{opt.desc}</div>
                </div>
              </label>
            ))}
          </fieldset>

          <div className="flex items-center justify-between gap-3 pt-1 text-xs">
            <label className="flex items-center gap-2 text-slate-300">
              <span>Coordinate System:</span>
              <select
                value={srid}
                onChange={(e) => setSrid(Number(e.target.value))}
                className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-100"
              >
                <option value={4326}>EPSG:4326 (WGS 84)</option>
                <option value={32643}>EPSG:32643 (UTM 43N)</option>
              </select>
            </label>

            <label className="flex items-center gap-1.5 text-slate-400 cursor-pointer">
              <input
                type="checkbox"
                checked={includeMergedSlivers}
                onChange={(e) => setIncludeMergedSlivers(e.target.checked)}
              />
              Include merged slivers
            </label>
          </div>

          <button
            type="button"
            disabled={isStaging}
            onClick={() =>
              onPresign({
                format,
                srid,
                include_merged_slivers: includeMergedSlivers,
              })
            }
            className="mt-1 rounded-lg bg-indigo-600 px-4 py-2 text-xs font-semibold text-white shadow hover:bg-indigo-500 disabled:opacity-50"
          >
            {isStaging ? "Staging in MinIO..." : "Generate Secure Download"}
          </button>
        </div>

        {stagedExport && (
          <div className="mt-4 rounded-xl border border-emerald-800/70 bg-emerald-950/30 p-3 text-xs flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-emerald-300">
                Staged Package Ready ({stagedExport.feature_count} parcels,{" "}
                {stagedExport.byte_size} bytes)
              </span>
              <a
                href={stagedExport.download_url}
                target="_blank"
                rel="noreferrer"
                className="rounded-md bg-emerald-600 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-500"
              >
                Download Package
              </a>
            </div>
            <div className="font-mono text-[10px] text-slate-300 break-all">
              Artifact SHA-256: {stagedExport.sha256}
            </div>
          </div>
        )}

        {auditManifest && (
          <div className="mt-3 rounded-xl border border-slate-800 bg-slate-950/70 p-3 text-[11px] text-slate-300 flex flex-col gap-1">
            <div className="font-semibold text-slate-200">
              Harmonization Audit Certificate (v{auditManifest.schema_version})
            </div>
            <div>
              Active Parcels: {auditManifest.parcels.active_parcels} | Merged Slivers:{" "}
              {auditManifest.parcels.merged_slivers} | Disputed:{" "}
              {auditManifest.parcels.disputed_parcels}
            </div>
            <div className="font-mono text-[10px] text-slate-400 break-all">
              Manifest SHA-256: {auditManifest.manifest_sha256}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
