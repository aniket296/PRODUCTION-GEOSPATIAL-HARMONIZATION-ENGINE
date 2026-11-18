import type { AutoResolveResponse } from "@/types/gis";

export interface AutoResolveBannerProps {
  unresolvedCount: number;
  isResolving: boolean;
  lastResult: AutoResolveResponse | null;
  onAutoResolve: () => Promise<void> | void;
}

export function AutoResolveBanner({
  unresolvedCount,
  isResolving,
  lastResult,
  onAutoResolve,
}: AutoResolveBannerProps) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-slate-800 bg-slate-900/90 p-4 shadow-xl text-slate-100">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-950/80 border border-indigo-800 text-indigo-400">
            <span className="text-base font-bold">⚡</span>
          </div>
          <div>
            <h3 className="text-xs font-semibold text-white">
              Deterministic Topological Healing
            </h3>
            <p className="text-[11px] text-slate-400">
              {unresolvedCount > 0
                ? `${unresolvedCount} topological defects detected`
                : "All planar topological defects resolved"}
            </p>
          </div>
        </div>

        <button
          onClick={() => onAutoResolve()}
          disabled={isResolving}
          className="flex items-center justify-center rounded-lg bg-gradient-to-r from-indigo-600 to-indigo-500 px-3.5 py-1.5 text-xs font-medium text-white shadow hover:from-indigo-500 hover:to-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
        >
          {isResolving ? "Healing Topology..." : "Auto-Heal Layer"}
        </button>
      </div>

      {lastResult && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="font-medium text-emerald-400">
              {lastResult.resolved_errors_count ?? lastResult.resolved_count ?? 0} defects resolved
            </span>
            <span className="font-mono text-[11px] text-amber-400">
              {lastResult.remaining_disputes_count ?? lastResult.skipped_disputed_count ?? 0} dispute locked
            </span>
          </div>

          <div className="grid grid-cols-3 gap-2 pt-1 border-t border-slate-900 text-[11px] text-slate-400">
            <div>
              Slivers Merged:{" "}
              <strong className="text-slate-200">
                {lastResult.slivers_merged_count ?? lastResult.slivers_merged ?? 0}
              </strong>
            </div>
            <div>
              Overlaps Trimmed:{" "}
              <strong className="text-slate-200">
                {lastResult.micro_overlaps_resolved_count ?? lastResult.overlaps_trimmed ?? 0}
              </strong>
            </div>
            <div>
              Gaps Closed:{" "}
              <strong className="text-slate-200">
                {lastResult.boundaries_snapped_count ?? lastResult.gaps_closed ?? 0}
              </strong>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
