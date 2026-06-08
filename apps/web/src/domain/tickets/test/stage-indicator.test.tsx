import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { WorkflowRunView } from "@core/api/public/queries";
import { StageIndicator } from "../StageIndicator";

/**
 * Smoke tests for StageIndicator. Pure-render component, no hooks — just
 * cover the three branches: hidden when empty, single-run line, and
 * multi-run chronological ordering.
 */

function run(workflow_name: string, state: string, id?: string): WorkflowRunView {
  return {
    id: id ?? `wfx-${workflow_name}`,
    workflow_name,
    workflow_version: "1",
    state,
    current_step_id: null,
    failure_reason: null,
    created_at: "2026-05-23T00:00:00Z",
    updated_at: "2026-05-23T00:00:00Z",
    steps: [],
  };
}

describe("StageIndicator", () => {
  it("renders nothing when runs is empty", () => {
    const { container } = render(<StageIndicator runs={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when runs is undefined", () => {
    const { container } = render(<StageIndicator runs={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a single run with workflow_name + state label", () => {
    render(<StageIndicator runs={[run("pr_review_v1", "running")]} />);
    expect(screen.getByTestId("stage-indicator")).toBeInTheDocument();
    expect(screen.getByTestId("stage-pr_review_v1")).toHaveTextContent(/pr_review_v1/i);
    expect(screen.getByTestId("stage-pr_review_v1")).toHaveTextContent(/Running/);
  });

  it("renders multi-run in chronological order (API returns oldest-first)", () => {
    render(
      <StageIndicator
        runs={[
          // oldest first (the wire format)
          run("pr_review_v1", "done", "wfx-1"),
          run("pr_review_v1", "running", "wfx-2"),
        ]}
      />,
    );
    const chips = screen.getAllByTestId(/^stage-pr_review_v1$/);
    // First chip → the older done run; second chip → the newer running run.
    expect(chips[0]).toHaveTextContent(/Done/);
    expect(chips[1]).toHaveTextContent(/Running/);
  });

  it("shows awaiting_human label when state is awaiting_human", () => {
    render(<StageIndicator runs={[run("pr_review_v1", "awaiting_human")]} />);
    expect(screen.getByTestId("stage-pr_review_v1")).toHaveTextContent(/Awaiting human/);
  });
});
