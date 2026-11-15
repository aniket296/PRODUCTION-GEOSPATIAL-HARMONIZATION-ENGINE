import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AutoResolveBanner } from "@/components/AutoResolveBanner";

describe("AutoResolveBanner", () => {
  it("renders trigger button and runs auto-resolve on click", () => {
    const onAutoResolve = vi.fn().mockResolvedValue(undefined);

    render(
      <AutoResolveBanner
        unresolvedCount={5}
        isResolving={false}
        lastResult={null}
        onAutoResolve={onAutoResolve}
      />
    );

    expect(screen.getByText(/5 topological defects detected/i)).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: /auto-heal layer/i });
    expect(btn).not.toBeDisabled();

    fireEvent.click(btn);
    expect(onAutoResolve).toHaveBeenCalled();
  });

  it("disables trigger button and indicates loading state when resolving", () => {
    render(
      <AutoResolveBanner
        unresolvedCount={5}
        isResolving={true}
        lastResult={null}
        onAutoResolve={vi.fn()}
      />
    );

    const btn = screen.getByRole("button", { name: /healing topology/i });
    expect(btn).toBeDisabled();
  });

  it("displays success message with resolved and protected dispute counts", () => {
    render(
      <AutoResolveBanner
        unresolvedCount={0}
        isResolving={false}
        lastResult={{
          layer_id: "layer-1",
          resolved_count: 4,
          skipped_disputed_count: 1,
          slivers_merged: 2,
          overlaps_trimmed: 1,
          gaps_closed: 1,
          verification_passed: true,
          message: "Topological healing executed successfully",
        }}
        onAutoResolve={vi.fn()}
      />
    );

    expect(screen.getByText(/4 defects resolved/i)).toBeInTheDocument();
    expect(screen.getByText(/1 dispute locked/i)).toBeInTheDocument();
  });
});
