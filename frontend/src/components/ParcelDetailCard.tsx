import type { ParcelGeoJSONFeature } from "@/lib/flatgeobufClient";

export interface ParcelDetailCardProps {
  parcel: ParcelGeoJSONFeature | null;
  onClear: () => void;
}

export function ParcelDetailCard({ parcel, onClear }: ParcelDetailCardProps) {
  if (!parcel) {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900/90 p-4 text-xs text-slate-400 shadow-xl">
        Select a parcel on the canvas to inspect geodetic metrics
      </div>
    );
  }

  const props = parcel.properties ?? {};
  const identifier = String(props.parcel_identifier ?? parcel.id ?? "Unknown");
  const areaSqm = Number(props.metric_area_sqm ?? 0);
  const perimeterM = Number(props.metric_perimeter_m ?? 0);
  const compactness = Number(props.polsby_popper_compactness ?? 0);
  const isLocked = Boolean(props.dispute_locked || props.is_disputed);
  const customAttrs =
    props.attributes && typeof props.attributes === "object"
      ? (props.attributes as Record<string, unknown>)
      : {};

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-slate-800 bg-slate-900/90 p-4 text-xs text-slate-100 shadow-xl">
      <div className="flex items-center justify-between border-b border-slate-800 pb-2.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm font-semibold text-white">
            {identifier}
          </span>
          {isLocked && (
            <span className="rounded bg-rose-950 px-2 py-0.5 text-[10px] font-bold tracking-wide text-rose-400 border border-rose-800">
              DISPUTE LOCKED
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={onClear}
          className="rounded px-2 py-0.5 text-[11px] text-slate-400 hover:bg-slate-800 hover:text-white"
        >
          Clear Selection
        </button>
      </div>

      <div className="grid grid-cols-3 gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2.5">
        <div>
          <div className="text-[10px] text-slate-400">Metric Area</div>
          <div className="font-mono font-semibold text-slate-100">
            {areaSqm.toFixed(2)} m²
          </div>
        </div>
        <div>
          <div className="text-[10px] text-slate-400">Perimeter</div>
          <div className="font-mono font-semibold text-slate-100">
            {perimeterM.toFixed(2)} m
          </div>
        </div>
        <div>
          <div className="text-[10px] text-slate-400">Compactness</div>
          <div className="font-mono font-semibold text-slate-100">
            {compactness.toFixed(3)}
          </div>
        </div>
      </div>

      {Object.keys(customAttrs).length > 0 && (
        <div className="flex flex-col gap-1 border-t border-slate-800 pt-2">
          <div className="text-[11px] font-medium text-slate-400">
            Cadastral Attributes
          </div>
          {Object.entries(customAttrs).map(([key, value]) => (
            <div key={key} className="flex items-center justify-between text-[11px]">
              <span className="text-slate-400">{key}:</span>
              <span className="font-mono text-slate-200">{String(value)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
