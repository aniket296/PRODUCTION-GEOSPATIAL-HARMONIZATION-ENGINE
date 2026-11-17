import { useState } from "react";
import { AutoResolveBanner } from "@/components/AutoResolveBanner";
import { DifferentialMapCanvas } from "@/components/DifferentialMapCanvas";
import { ExportModal } from "@/components/ExportModal";
import { LayerUploadPanel } from "@/components/LayerUploadPanel";
import { ParcelDetailCard } from "@/components/ParcelDetailCard";
import { TopologyInspectorPanel } from "@/components/TopologyInspectorPanel";
import { WorkbenchHeader } from "@/components/WorkbenchHeader";
import {
  autoResolveLayer,
  getExportManifest,
  getLayerStatus,
  getTopologyErrors,
  presignLayerExport,
  uploadLayer,
  type UploadLayerParams,
} from "@/lib/api";
import type { ParcelGeoJSONFeatureCollection } from "@/lib/flatgeobufClient";
import { useGisStore } from "@/store/gisStore";
import type { LayerExportPresignRequest } from "@/types/gis";

export function App() {
  const {
    activeLayerId,
    layerStatus,
    topologyErrors,
    viewMode,
    swipePosition,
    severityFilter,
    typeFilter,
    showResolvedErrors,
    selectedErrorId,
    selectedParcelId,
    isResolving,
    lastResolveResult,
    stagedExport,
    auditManifest,
    errorBanner,
    setActiveLayer,
    setLayerStatus,
    setTopologyErrors,
    setViewMode,
    setSwipePosition,
    setSeverityFilter,
    setTypeFilter,
    setShowResolvedErrors,
    setSelectedErrorId,
    setSelectedParcelId,
    setIsResolving,
    setLastResolveResult,
    setStagedExport,
    setAuditManifest,
    setErrorBanner,
    getFilteredTopologyErrors,
    resetWorkbench,
  } = useGisStore();

  const [isUploading, setIsUploading] = useState(false);
  const [isExportOpen, setIsExportOpen] = useState(false);
  const [isStagingExport, setIsStagingExport] = useState(false);
  const [parcels] = useState<ParcelGeoJSONFeatureCollection | null>(null);

  const handleUpload = async (params: UploadLayerParams) => {
    setIsUploading(true);
    setErrorBanner(null);
    try {
      const uploaded = await uploadLayer(params);
      setActiveLayer(uploaded.layer_id);
      const [status, errors] = await Promise.all([
        getLayerStatus(uploaded.layer_id),
        getTopologyErrors(uploaded.layer_id),
      ]);
      setLayerStatus(status);
      setTopologyErrors(errors.features ?? []);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : "Failed to upload layer");
    } finally {
      setIsUploading(false);
    }
  };

  const handleAutoResolve = async () => {
    if (!activeLayerId) return;
    setIsResolving(true);
    setErrorBanner(null);
    try {
      const result = await autoResolveLayer(activeLayerId);
      setLastResolveResult(result);
      const refreshedErrors = await getTopologyErrors(activeLayerId);
      setTopologyErrors(refreshedErrors.features ?? []);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : "Failed to auto-heal topology");
    } finally {
      setIsResolving(false);
    }
  };

  const handlePresignExport = async (request: LayerExportPresignRequest) => {
    if (!activeLayerId) return;
    setIsStagingExport(true);
    setErrorBanner(null);
    try {
      const [staged, manifest] = await Promise.all([
        presignLayerExport(activeLayerId, request),
        getExportManifest(activeLayerId),
      ]);
      setStagedExport(staged);
      setAuditManifest(manifest);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : "Failed to stage export");
    } finally {
      setIsStagingExport(false);
    }
  };

  const filteredErrors = getFilteredTopologyErrors();
  const unresolvedCount = topologyErrors.filter((e) => !e.properties.is_resolved).length;
  const selectedParcel =
    parcels?.features.find(
      (f) =>
        String(f.properties?.parcel_identifier ?? f.id) === selectedParcelId
    ) ?? null;

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-slate-950 text-slate-100">
      <WorkbenchHeader
        activeLayerId={activeLayerId}
        layerStatus={layerStatus}
        onOpenExport={() => setIsExportOpen(true)}
        onResetWorkbench={() => {
          resetWorkbench();
          setIsExportOpen(false);
        }}
      />

      {errorBanner && (
        <div
          role="alert"
          className="border-b border-rose-800 bg-rose-950/90 px-6 py-2 text-xs text-rose-200"
        >
          {errorBanner}
        </div>
      )}

      <main className="grid flex-1 grid-cols-12 gap-4 overflow-hidden p-4">
        {/* Left Control Column */}
        <section className="col-span-3 flex flex-col gap-4 overflow-y-auto pr-1">
          <LayerUploadPanel
            onUpload={handleUpload}
            layerStatus={layerStatus}
            isUploading={isUploading}
          />
          <AutoResolveBanner
            unresolvedCount={unresolvedCount}
            isResolving={isResolving}
            lastResult={lastResolveResult}
            onAutoResolve={handleAutoResolve}
          />
          <ParcelDetailCard
            parcel={selectedParcel}
            onClear={() => setSelectedParcelId(null)}
          />
        </section>

        {/* Center Differential Map Canvas */}
        <section className="col-span-6 flex h-full flex-col overflow-hidden">
          <DifferentialMapCanvas
            parcels={parcels}
            topologyErrors={filteredErrors}
            viewMode={viewMode}
            swipePosition={swipePosition}
            selectedErrorId={selectedErrorId}
            selectedParcelId={selectedParcelId}
            onSwipeChange={setSwipePosition}
            onViewModeChange={setViewMode}
            onSelectParcel={setSelectedParcelId}
            onSelectError={setSelectedErrorId}
          />
        </section>

        {/* Right Topology Inspector Column */}
        <section className="col-span-3 flex h-full flex-col overflow-hidden">
          <TopologyInspectorPanel
            errors={filteredErrors}
            selectedErrorId={selectedErrorId}
            severityFilter={severityFilter}
            typeFilter={typeFilter}
            showResolvedErrors={showResolvedErrors}
            onSelectError={setSelectedErrorId}
            onSeverityChange={setSeverityFilter}
            onTypeChange={setTypeFilter}
            onShowResolvedChange={setShowResolvedErrors}
          />
        </section>
      </main>

      <ExportModal
        isOpen={isExportOpen}
        onClose={() => setIsExportOpen(false)}
        onPresign={handlePresignExport}
        stagedExport={stagedExport}
        auditManifest={auditManifest}
        isStaging={isStagingExport}
      />
    </div>
  );
}

export default App;
