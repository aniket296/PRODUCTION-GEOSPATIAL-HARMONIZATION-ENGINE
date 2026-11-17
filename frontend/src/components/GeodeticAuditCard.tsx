export interface GeodeticAuditCardProps {
  sourceCrs?: string | null;
  targetCrs?: string | null;
  metadata?: Record<string, unknown> | null;
}

export function GeodeticAuditCard({
  sourceCrs,
  targetCrs = "EPSG:4326",
  metadata,
}: GeodeticAuditCardProps) {
  if (!metadata || Object.keys(metadata).length === 0) {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900/90 p-4 text-xs text-slate-400 shadow-xl">
        Awaiting survey geodetic calibration
      </div>
    );
  }

  const rmse = Number(metadata.rmse_meters ?? 0);
  const gcpCount = Number(metadata.gcp_count ?? 0);
  const tpsActive = Boolean(metadata.thin_plate_spline_applied);

  return (
    <div className="flex flex-col gap-2.5 rounded-xl border border-slate-800 bg-slate-900/90 p-4 text-xs text-slate-100 shadow-xl">
      <div className="flex items-center justify-between border-b border-slate-800 pb-2">
        <span className="font-semibold text-white">Geodetic Calibration</span>
        <span className="font-mono text-[11px] text-indigo-300">
          {sourceCrs ?? "SOURCE"} → {targetCrs ?? "EPSG:4326"}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2.5">
        <div>
          <div className="text-[10px] text-slate-400">Residual RMSE</div>
          <div className="font-mono font-semibold text-emerald-400">
            {rmse.toFixed(3)} m
          </div>
        </div>
        <div>
          <div className="text-[10px] text-slate-400">Control Points</div>
          <div className="font-mono font-semibold text-slate-100">
            {gcpCount} GCPs
          </div>
        </div>
      </div>

      {tpsActive && (
        <div className="rounded border border-indigo-800/60 bg-indigo-950/40 px-2.5 py-1 text-[11px] font-medium text-indigo-300">
          TPS Non-Linear Warp Active
        </div>
      )}
    </div>
  );
}
