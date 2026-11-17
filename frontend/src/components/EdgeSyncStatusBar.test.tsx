import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EdgeSyncStatusBar } from "@/components/EdgeSyncStatusBar";

describe("EdgeSyncStatusBar", () => {
  it("displays offline status when no edge workers are registered", () => {
    render(
      <EdgeSyncStatusBar
        healthyWorkers={0}
        totalWorkers={0}
        pendingDeltas={0}
        conflictCount={0}
        onRefresh={vi.fn()}
      />
    );

    expect(screen.getByText(/Edge Cluster: Offline/i)).toBeInTheDocument();
  });

  it("displays active worker ratio, queued CRDT deltas, conflicts, and triggers refresh", () => {
    const onRefresh = vi.fn();

    render(
      <EdgeSyncStatusBar
        healthyWorkers={3}
        totalWorkers={4}
        pendingDeltas={2}
        conflictCount={1}
        onRefresh={onRefresh}
      />
    );

    expect(screen.getByText(/Edge Cluster: 3\/4 Online/i)).toBeInTheDocument();
    expect(screen.getByText(/2 deltas queued/i)).toBeInTheDocument();
    expect(screen.getByText(/1 CRDT conflict/i)).toBeInTheDocument();

    const syncBtn = screen.getByRole("button", { name: /sync telemetry/i });
    fireEvent.click(syncBtn);
    expect(onRefresh).toHaveBeenCalledOnce();
  });
});
