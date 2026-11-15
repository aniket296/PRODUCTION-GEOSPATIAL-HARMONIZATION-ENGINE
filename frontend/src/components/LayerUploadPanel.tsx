import React, { useState } from "react";
import type { LayerStatusResponse } from "@/types/gis";
import type { UploadLayerParams } from "@/lib/api";

export interface LayerUploadPanelProps {
  onUpload: (params: UploadLayerParams) => Promise<void> | void;
  layerStatus: LayerStatusResponse | null;
  isUploading: boolean;
}

const SUPPORTED_CRS_OPTIONS = [
  { value: "EPSG:4326", label: "WGS 84 (EPSG:4326)" },
  { value: "EPSG:32643", label: "WGS 84 / UTM zone 43N (EPSG:32643)" },
  { value: "EPSG:32644", label: "WGS 84 / UTM zone 44N (EPSG:32644)" },
  { value: "EPSG:3857", label: "Pseudo-Mercator (EPSG:3857)" },
  { value: "EPSG:7755", label: "Kalyanpur 1975 / India Zone I (EPSG:7755)" },
];

export function LayerUploadPanel({
  onUpload,
  layerStatus,
  isUploading,
}: LayerUploadPanelProps) {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [layerName, setLayerName] = useState("");
  const [declaredCrs, setDeclaredCrs] = useState("EPSG:32643");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedFile) return;

    await onUpload({
      file: selectedFile,
      name: layerName.trim() || undefined,
      declaredCrs,
    });
  };

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/90 p-5 shadow-xl text-slate-100 flex flex-col gap-4">
      <div className="border-b border-slate-800 pb-3">
        <h2 className="text-base font-semibold tracking-tight text-white flex items-center gap-2">
          <span>Cadastral Layer Ingestion</span>
        </h2>
        <p className="text-xs text-slate-400 mt-0.5">
          Upload DXF, GeoJSON, Shapefile (zip), or GeoTIFF surveys
        </p>
      </div>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div>
          <label
            htmlFor="layer-name-input"
            className="block text-xs font-medium text-slate-300 mb-1"
          >
            Layer Name
          </label>
          <input
            id="layer-name-input"
            type="text"
            value={layerName}
            onChange={(e) => setLayerName(e.target.value)}
            placeholder="e.g. Ward 14 Cadastral Survey"
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-100 placeholder-slate-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>

        <div>
          <label
            htmlFor="declared-crs-select"
            className="block text-xs font-medium text-slate-300 mb-1"
          >
            Declared CRS
          </label>
          <select
            id="declared-crs-select"
            value={declaredCrs}
            onChange={(e) => setDeclaredCrs(e.target.value)}
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            {SUPPORTED_CRS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="cadastral-file-input"
            className="block text-xs font-medium text-slate-300 mb-1"
          >
            Cadastral File
          </label>
          <input
            id="cadastral-file-input"
            type="file"
            accept=".dxf,.geojson,.json,.zip,.shp,.tif,.tiff"
            onChange={(e) => {
              const file = e.target.files?.[0] || null;
              setSelectedFile(file);
              if (file && !layerName) {
                const baseName = file.name.replace(/\.[^/.]+$/, "");
                setLayerName(baseName);
              }
            }}
            className="w-full text-xs text-slate-400 file:mr-2 file:rounded-md file:border-0 file:bg-slate-800 file:px-2.5 file:py-1 file:text-xs file:font-medium file:text-slate-200 hover:file:bg-slate-700 cursor-pointer"
          />
        </div>

        <button
          type="submit"
          disabled={!selectedFile || isUploading}
          className="mt-1 flex items-center justify-center rounded-md bg-indigo-600 px-3 py-2 text-xs font-medium text-white shadow hover:bg-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 focus:ring-offset-slate-900 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {isUploading ? "Uploading..." : "Ingest Survey Layer"}
        </button>
      </form>

      {layerStatus && (
        <div className="mt-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="font-medium text-slate-300">Status:</span>
            <span
              className={`font-semibold px-2 py-0.5 rounded text-[10px] tracking-wide uppercase ${
                layerStatus.status === "COMPLETED"
                  ? "bg-emerald-950 text-emerald-400 border border-emerald-800"
                  : layerStatus.status === "FAILED"
                  ? "bg-rose-950 text-rose-400 border border-rose-800"
                  : "bg-amber-950 text-amber-400 border border-amber-800"
              }`}
            >
              {layerStatus.status}
            </span>
          </div>

          <div className="flex items-center justify-between">
            <span className="text-slate-400">Progress:</span>
            <span className="font-mono text-slate-200">
              {layerStatus.progress_percent}%
            </span>
          </div>

          <div className="w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-indigo-500 h-1.5 transition-all duration-300"
              style={{ width: `${layerStatus.progress_percent}%` }}
            />
          </div>

          <div className="flex items-center justify-between pt-1 border-t border-slate-900">
            <span className="text-slate-400">Features:</span>
            <span className="font-mono text-slate-200">
              {layerStatus.feature_count}
            </span>
          </div>

          {layerStatus.detected_crs && (
            <div className="flex items-center justify-between">
              <span className="text-slate-400">CRS:</span>
              <span className="font-mono text-slate-300 text-[11px]">
                {layerStatus.detected_crs}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
