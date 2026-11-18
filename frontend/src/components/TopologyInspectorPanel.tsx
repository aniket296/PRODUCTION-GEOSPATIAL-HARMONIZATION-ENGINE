import type {
  ErrorTypeFilter,
  SeverityFilter,
} from "@/store/gisStore";
import type {
  TopologyErrorFeature,
  TopologySeverity,
} from "@/types/gis";

export interface TopologyInspectorPanelProps {
  errors: TopologyErrorFeature[];
  selectedErrorId: string | null;
  severityFilter: SeverityFilter;
  typeFilter: ErrorTypeFilter;
  showResolvedErrors: boolean;
  onSelectError: (errorId: string) => void;
  onSeverityChange: (severity: SeverityFilter) => void;
  onTypeChange: (type: ErrorTypeFilter) => void;
  onShowResolvedChange: (show: boolean) => void;
}

const SEVERITY_CONFIG: Record<
  TopologySeverity,
  { label: string; bg: string; text: string; border: string }
> = {
  AUTO_RESOLVABLE: {
    label: "Auto-Resolvable",
    bg: "bg-emerald-950/60",
    text: "text-emerald-400",
    border: "border-emerald-800",
  },
  CRITICAL_DISPUTE: {
    label: "Critical Dispute",
    bg: "bg-rose-950/60",
    text: "text-rose-400",
    border: "border-rose-800",
  },
  WARNING: {
    label: "Warning",
    bg: "bg-amber-950/60",
    text: "text-amber-400",
    border: "border-amber-800",
  },
  INFO: {
    label: "Info",
    bg: "bg-slate-800",
    text: "text-slate-300",
    border: "border-slate-700",
  },
};

export function TopologyInspectorPanel({
  errors,
  selectedErrorId,
  severityFilter,
  typeFilter,
  showResolvedErrors,
  onSelectError,
  onSeverityChange,
  onTypeChange,
  onShowResolvedChange,
}: TopologyInspectorPanelProps) {
  const unresolvedCount = errors.filter((e) => !e.properties.is_resolved).length;

  return (
    <div className="flex h-full flex-col rounded-xl border border-slate-800 bg-slate-900/90 shadow-xl text-slate-100 overflow-hidden">
      {/* Header */}
      <div className="border-b border-slate-800 p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold tracking-tight text-white">
              Topology Defects
            </h2>
            <span className="rounded-full bg-rose-950 px-2 py-0.5 text-[11px] font-semibold text-rose-400 border border-rose-800">
              {errors.length}
            </span>
          </div>
          <span className="text-xs text-slate-400">
            {unresolvedCount} active
          </span>
        </div>

        {/* Filter Toolbar */}
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          <select
            value={severityFilter}
            onChange={(e) => onSeverityChange(e.target.value as SeverityFilter)}
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-slate-300 focus:border-indigo-500 focus:outline-none"
          >
            <option value="ALL">All Severities</option>
            <option value="CRITICAL_DISPUTE">Critical Dispute</option>
            <option value="AUTO_RESOLVABLE">Auto-Resolvable</option>
            <option value="WARNING">Warning</option>
            <option value="INFO">Info</option>
          </select>

          <select
            value={typeFilter}
            onChange={(e) => onTypeChange(e.target.value as ErrorTypeFilter)}
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-slate-300 focus:border-indigo-500 focus:outline-none"
          >
            <option value="ALL">All Defect Types</option>
            <option value="OVERLAP">OVERLAP</option>
            <option value="SLIVER">SLIVER</option>
            <option value="GAP">GAP</option>
            <option value="DANGLING_NODE">DANGLING_NODE</option>
            <option value="DUPLICATE_VERTEX">DUPLICATE_VERTEX</option>
            <option value="INVALID_GEOMETRY">INVALID_GEOMETRY</option>
          </select>

          <label className="flex items-center gap-1.5 text-xs text-slate-400 cursor-pointer pl-1">
            <input
              type="checkbox"
              checked={showResolvedErrors}
              onChange={(e) => onShowResolvedChange(e.target.checked)}
              className="rounded border-slate-700 bg-slate-950 text-indigo-600 focus:ring-0"
            />
            Show Resolved
          </label>
        </div>
      </div>

      {/* Error List */}
      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-2">
        {errors.length === 0 ? (
          <div className="flex h-32 flex-col items-center justify-center rounded-lg border border-dashed border-slate-800 p-4 text-center">
            <p className="text-xs font-medium text-slate-400">
              No topological defects detected
            </p>
            <p className="text-[11px] text-slate-600 mt-1">
              All parcels satisfy strict non-overlapping planar topology
            </p>
          </div>
        ) : (
          errors.map((feature, idx) => {
            const props = feature.properties;
            const errId = String(props.error_id ?? feature.id ?? `err-${idx + 1}`);
            const isSelected = selectedErrorId === errId;
            const sevConfig =
              SEVERITY_CONFIG[props.severity as TopologySeverity] ||
              SEVERITY_CONFIG.INFO;

            return (
              <div
                key={errId}
                onClick={() => onSelectError(errId)}
                className={`cursor-pointer rounded-lg border p-3 text-xs transition-all ${
                  isSelected
                    ? "border-indigo-500 bg-indigo-950/40 shadow-md ring-1 ring-indigo-500"
                    : "border-slate-800 bg-slate-950/60 hover:border-slate-700 hover:bg-slate-950"
                } ${props.is_resolved ? "opacity-60" : ""}`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-1.5 font-medium">
                    <span className="font-mono text-slate-200">
                      {props.error_type}
                    </span>
                    {props.is_resolved && (
                      <span className="rounded bg-emerald-950 px-1.5 py-0.2 text-[10px] text-emerald-400 border border-emerald-800">
                        RESOLVED
                      </span>
                    )}
                  </div>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-semibold border ${sevConfig.bg} ${sevConfig.text} ${sevConfig.border}`}
                  >
                    {sevConfig.label}
                  </span>
                </div>

                {props.description && (
                  <p className="mt-1.5 text-slate-300 text-[11px] leading-relaxed">
                    {props.description}
                  </p>
                )}

                <div className="mt-2 flex items-center justify-between text-[11px] text-slate-400 pt-1.5 border-t border-slate-900">
                  <span>
                    Area:{" "}
                    <strong className="text-slate-300">
                      {props.affected_area_sqm != null
                        ? `${props.affected_area_sqm.toFixed(2)} m²`
                        : "N/A"}
                    </strong>
                  </span>
                  {props.primary_parcel_id && (
                    <span className="font-mono text-[10px] text-slate-400">
                      ID: {props.primary_parcel_id}
                    </span>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
