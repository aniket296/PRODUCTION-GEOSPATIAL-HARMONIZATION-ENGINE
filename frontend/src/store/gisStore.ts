import { create } from "zustand";
import type {
  AutoResolveResponse,
  HarmonizationAuditManifest,
  LayerExportPresignResponse,
  LayerStatusResponse,
  TopologyErrorFeature,
  TopologyErrorType,
  TopologySeverity,
} from "@/types/gis";

export type MapViewMode = "swipe" | "overlay" | "split";
export type SeverityFilter = "ALL" | TopologySeverity;
export type ErrorTypeFilter = "ALL" | TopologyErrorType;

export interface GisWorkbenchState {
  activeLayerId: string | null;
  layerStatus: LayerStatusResponse | null;
  topologyErrors: TopologyErrorFeature[];
  viewMode: MapViewMode;
  swipePosition: number;
  severityFilter: SeverityFilter;
  typeFilter: ErrorTypeFilter;
  showResolvedErrors: boolean;
  selectedErrorId: string | null;
  selectedParcelId: string | null;
  isResolving: boolean;
  lastResolveResult: AutoResolveResponse | null;
  stagedExport: LayerExportPresignResponse | null;
  auditManifest: HarmonizationAuditManifest | null;
  errorBanner: string | null;

  setActiveLayer: (layerId: string | null) => void;
  setLayerStatus: (status: LayerStatusResponse | null) => void;
  setTopologyErrors: (errors: TopologyErrorFeature[]) => void;
  setViewMode: (mode: MapViewMode) => void;
  setSwipePosition: (position: number) => void;
  setSeverityFilter: (severity: SeverityFilter) => void;
  setTypeFilter: (errorType: ErrorTypeFilter) => void;
  setShowResolvedErrors: (show: boolean) => void;
  setSelectedErrorId: (errorId: string | null) => void;
  setSelectedParcelId: (parcelId: string | null) => void;
  setIsResolving: (isResolving: boolean) => void;
  setLastResolveResult: (result: AutoResolveResponse | null) => void;
  setStagedExport: (staged: LayerExportPresignResponse | null) => void;
  setAuditManifest: (manifest: HarmonizationAuditManifest | null) => void;
  setErrorBanner: (message: string | null) => void;
  getFilteredTopologyErrors: () => TopologyErrorFeature[];
  resetWorkbench: () => void;
}

const initialState = {
  activeLayerId: null,
  layerStatus: null,
  topologyErrors: [] as TopologyErrorFeature[],
  viewMode: "swipe" as MapViewMode,
  swipePosition: 50,
  severityFilter: "ALL" as SeverityFilter,
  typeFilter: "ALL" as ErrorTypeFilter,
  showResolvedErrors: false,
  selectedErrorId: null,
  selectedParcelId: null,
  isResolving: false,
  lastResolveResult: null,
  stagedExport: null,
  auditManifest: null,
  errorBanner: null,
};

export const useGisStore = create<GisWorkbenchState>((set, get) => ({
  ...initialState,

  setActiveLayer: (layerId) =>
    set({
      activeLayerId: layerId,
      selectedErrorId: null,
      selectedParcelId: null,
      lastResolveResult: null,
      stagedExport: null,
      auditManifest: null,
    }),

  setLayerStatus: (layerStatus) => set({ layerStatus }),

  setTopologyErrors: (topologyErrors) => set({ topologyErrors }),

  setViewMode: (viewMode) => set({ viewMode }),

  setSwipePosition: (position) =>
    set({
      swipePosition: Math.max(0, Math.min(100, Math.round(position))),
    }),

  setSeverityFilter: (severityFilter) => set({ severityFilter }),

  setTypeFilter: (typeFilter) => set({ typeFilter }),

  setShowResolvedErrors: (showResolvedErrors) => set({ showResolvedErrors }),

  setSelectedErrorId: (selectedErrorId) => set({ selectedErrorId }),

  setSelectedParcelId: (selectedParcelId) => set({ selectedParcelId }),

  setIsResolving: (isResolving) => set({ isResolving }),

  setLastResolveResult: (lastResolveResult) => set({ lastResolveResult }),

  setStagedExport: (stagedExport) => set({ stagedExport }),

  setAuditManifest: (auditManifest) => set({ auditManifest }),

  setErrorBanner: (errorBanner) => set({ errorBanner }),

  getFilteredTopologyErrors: () => {
    const { topologyErrors, severityFilter, typeFilter, showResolvedErrors } = get();
    return topologyErrors.filter((feature) => {
      const props = feature.properties;
      if (!showResolvedErrors && props.is_resolved) {
        return false;
      }
      if (severityFilter !== "ALL" && props.severity !== severityFilter) {
        return false;
      }
      if (typeFilter !== "ALL" && props.error_type !== typeFilter) {
        return false;
      }
      return true;
    });
  },

  resetWorkbench: () => set({ ...initialState }),
}));
