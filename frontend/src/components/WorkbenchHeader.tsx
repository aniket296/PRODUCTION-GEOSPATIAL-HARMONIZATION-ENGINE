import type { LayerStatusResponse } from "@/types/gis";

export interface WorkbenchHeaderProps {
  activeLayerId: string | null;
  layerStatus: LayerStatusResponse | null;
  onOpenExport: () => void;
  onResetWorkbench: () => void;
}

export function WorkbenchHeader({
  activeLayerId,
  layerStatus,
  onOpenExport,
  onResetWorkbench,
}: WorkbenchHeaderProps) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 bg-slate-900/95 px-6 py-3 text-slate-100 shadow-lg">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-600 font-bold text-white shadow">
          GH
        </div>
        <div>
          <h1 className="text-sm font-semibold tracking-tight text-white">
            Geospatial Harmonization Engine
          </h1>
          <p className="text-[11px] text-slate-400">
            SIH26013 • Multi-Source Urban Land Records Differential GIS Workbench
          </p>
        </div>
      </div>

      <div className="flex items-center gap-3 text-xs">
        {layerStatus ? (
          <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950 px-3 py-1.5">
            <span className="font-medium text-slate-200">
              {layerStatus.name || layerStatus.layer_id}
            </span>
            <span className="text-slate-600">•</span>
            <span className="font-mono text-emerald-400">
              {layerStatus.feature_count} features
            </span>
            {layerStatus.detected_crs && (
              <>
                <span className="text-slate-600">•</span>
                <span className="font-mono text-[11px] text-slate-400">
                  {layerStatus.detected_crs}
                </span>
              </>
            )}
          </div>
        ) : (
          <span className="rounded-lg border border-slate-800 bg-slate-950 px-3 py-1.5 text-slate-400">
            No Active Layer
          </span>
        )}

        <button
          type="button"
          onClick={onResetWorkbench}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 font-medium text-slate-200 hover:bg-slate-700"
        >
          Reset Workbench
        </button>

        <button
          type="button"
          disabled={!activeLayerId}
          onClick={onOpenExport}
          className="rounded-lg bg-emerald-600 px-3.5 py-1.5 font-semibold text-white shadow hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Export Package
        </button>
      </div>
    </header>
  );
}
