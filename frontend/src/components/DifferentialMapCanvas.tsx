import type { ParcelGeoJSONFeatureCollection } from "@/lib/flatgeobufClient";
import type { MapViewMode } from "@/store/gisStore";
import type { TopologyErrorFeature } from "@/types/gis";

export interface DifferentialMapCanvasProps {
  parcels: ParcelGeoJSONFeatureCollection | null;
  topologyErrors: TopologyErrorFeature[];
  viewMode: MapViewMode;
  swipePosition: number;
  selectedErrorId: string | null;
  selectedParcelId: string | null;
  onSwipeChange: (position: number) => void;
  onViewModeChange: (mode: MapViewMode) => void;
  onSelectParcel: (parcelId: string) => void;
  onSelectError: (errorId: string) => void;
}

const VIEW_MODES: { value: MapViewMode; label: string }[] = [
  { value: "swipe", label: "Swipe" },
  { value: "overlay", label: "Overlay" },
  { value: "split", label: "Split" },
];

export function DifferentialMapCanvas({
  parcels,
  topologyErrors,
  viewMode,
  swipePosition,
  selectedErrorId,
  selectedParcelId,
  onSwipeChange,
  onViewModeChange,
  onSelectParcel,
  onSelectError,
}: DifferentialMapCanvasProps) {
  const parcelList = parcels?.features ?? [];
  const activeDefects = topologyErrors.filter((e) => !e.properties.is_resolved);

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-950 text-slate-100 shadow-2xl">
      {/* Top HUD Bar */}
      <div className="z-20 flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 bg-slate-900/90 px-4 py-2.5 backdrop-blur">
        <div className="flex items-center gap-3 text-xs">
          <span className="rounded-md bg-emerald-950/70 px-2.5 py-1 font-medium text-emerald-300 border border-emerald-800">
            {parcelList.length} Harmonized Parcels
          </span>
          <span className="rounded-md bg-rose-950/70 px-2.5 py-1 font-medium text-rose-300 border border-rose-800">
            {activeDefects.length} Active Defects
          </span>
        </div>

        <div className="flex items-center gap-1.5 rounded-lg border border-slate-800 bg-slate-950 p-1">
          {VIEW_MODES.map((m) => (
            <button
              key={m.value}
              type="button"
              onClick={() => onViewModeChange(m.value)}
              className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                viewMode === m.value
                  ? "bg-indigo-600 text-white shadow"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {/* Differential Viewport */}
      <div className="relative flex-1 overflow-hidden bg-[radial-gradient(#1e293b_1px,transparent_1px)] [background-size:20px_20px]">
        {/* Raw Survey Layer (Left / Base) */}
        <div
          data-testid="raw-layer-pane"
          className="absolute inset-0 flex flex-col justify-start p-4 text-xs text-amber-300/80"
        >
          <div className="inline-flex w-fit items-center gap-2 rounded bg-slate-900/80 px-2.5 py-1 text-[11px] font-mono border border-amber-800/50">
            RAW CADASTRE (SOURCE SURVEY)
          </div>
        </div>

        {/* Harmonized Layer (Right / Clipped in Swipe mode) */}
        <div
          data-testid="harmonized-layer-pane"
          style={
            viewMode === "swipe"
              ? { clipPath: `inset(0 0 0 ${swipePosition}%)` }
              : undefined
          }
          className="absolute inset-0 flex flex-col justify-end bg-indigo-950/15 p-4 text-xs text-emerald-300"
        >
          <div className="ml-auto inline-flex w-fit items-center gap-2 rounded bg-slate-900/80 px-2.5 py-1 text-[11px] font-mono border border-emerald-800/50">
            HARMONIZED WGS 84 (EPSG:4326)
          </div>
        </div>

        {/* Interactive Feature & Defect Inspector Chips */}
        <div className="relative z-10 flex h-full flex-col items-center justify-center gap-4 p-6">
          <div className="flex flex-wrap items-center justify-center gap-2">
            {parcelList.map((feature, idx) => {
              const ident = String(
                feature.properties?.parcel_identifier ?? feature.id ?? `P-${idx + 1}`
              );
              const isSelected = selectedParcelId === ident;
              return (
                <button
                  key={ident}
                  type="button"
                  onClick={() => onSelectParcel(ident)}
                  className={`rounded-lg border px-3 py-1.5 font-mono text-xs transition-all ${
                    isSelected
                      ? "border-emerald-400 bg-emerald-950/80 text-emerald-200 ring-2 ring-emerald-500"
                      : "border-emerald-800/70 bg-slate-900/85 text-emerald-300 hover:border-emerald-500"
                  }`}
                >
                  {ident}
                </button>
              );
            })}

            {topologyErrors.map((err, idx) => {
              const errId = String(
                err.properties.error_id ?? err.id ?? `err-${idx + 1}`
              );
              const isSelected = selectedErrorId === errId;
              return (
                <button
                  key={errId}
                  type="button"
                  onClick={() => onSelectError(errId)}
                  className={`rounded-lg border px-2.5 py-1 font-mono text-[11px] transition-all ${
                    isSelected
                      ? "border-rose-400 bg-rose-950/90 text-rose-200 ring-2 ring-rose-500"
                      : "border-rose-800/70 bg-slate-900/85 text-rose-300 hover:border-rose-500"
                  }`}
                >
                  {err.properties.error_type}:{errId}
                </button>
              );
            })}
          </div>
        </div>

        {/* Vertical Swipe Divider Line */}
        {viewMode === "swipe" && (
          <div
            style={{ left: `${swipePosition}%` }}
            className="pointer-events-none absolute inset-y-0 z-20 w-0.5 bg-indigo-400 shadow-[0_0_12px_rgba(99,102,241,0.9)]"
          />
        )}
      </div>

      {/* Bottom Swipe Control Bar */}
      <div className="z-20 flex items-center gap-4 border-t border-slate-800 bg-slate-900/90 px-4 py-2.5 text-xs">
        <span className="font-mono text-[11px] text-amber-300">Raw Survey</span>
        <input
          aria-label="Swipe divider position"
          type="range"
          min={0}
          max={100}
          value={swipePosition}
          onChange={(e) => onSwipeChange(Number(e.target.value))}
          className="h-1.5 flex-1 cursor-pointer appearance-none rounded-lg bg-slate-800 accent-indigo-500"
        />
        <span className="font-mono text-[11px] text-emerald-300">
          Harmonized ({swipePosition}%)
        </span>
      </div>
    </div>
  );
}
