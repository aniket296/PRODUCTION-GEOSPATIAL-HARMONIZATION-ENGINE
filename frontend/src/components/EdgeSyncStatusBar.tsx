export interface EdgeSyncStatusBarProps {
  healthyWorkers: number;
  totalWorkers: number;
  pendingDeltas: number;
  conflictCount: number;
  onRefresh: () => void;
}

export function EdgeSyncStatusBar({
  healthyWorkers,
  totalWorkers,
  pendingDeltas,
  conflictCount,
  onRefresh,
}: EdgeSyncStatusBarProps) {
  const isOffline = totalWorkers === 0 && healthyWorkers === 0;

  return (
    <footer className="flex flex-wrap items-center justify-between gap-4 border-t border-slate-800 bg-slate-900/95 px-6 py-2 text-xs text-slate-300">
      <div className="flex items-center gap-4">
        <span className="flex items-center gap-2 font-mono text-[11px]">
          <span
            className={`h-2 w-2 rounded-full ${
              isOffline ? "bg-slate-500" : "bg-emerald-400"
            }`}
          />
          {isOffline
            ? "Edge Cluster: Offline"
            : `Edge Cluster: ${healthyWorkers}/${totalWorkers} Online`}
        </span>

        <span className="font-mono text-[11px] text-slate-400">
          {pendingDeltas} deltas queued
        </span>

        {conflictCount > 0 && (
          <span className="rounded bg-amber-950 px-2 py-0.5 font-mono text-[10px] font-semibold text-amber-300 border border-amber-800">
            {conflictCount} CRDT conflict{conflictCount > 1 ? "s" : ""}
          </span>
        )}
      </div>

      <button
        type="button"
        onClick={onRefresh}
        className="rounded border border-slate-700 bg-slate-800 px-2.5 py-1 text-[11px] font-medium text-slate-200 hover:bg-slate-700"
      >
        Sync Telemetry
      </button>
    </footer>
  );
}
