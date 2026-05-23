import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

/**
 * Smoke tests for the M06 TicketDetailPage. Mocks every hook the page
 * touches; asserts that the header, stage indicator, and tab strip
 * render against a representative ticket.
 */

vi.mock("@tanstack/react-router", () => ({
  useParams: () => ({ ticketId: "t1" }),
}));

vi.mock("@core/api", () => ({
  useTicket: () => ({
    data: {
      id: "t1",
      org_id: "o1",
      source: "github_pr",
      source_external_id: "x/y#1",
      title: "Add /metrics endpoint",
      description: null,
      status: "in_review",
      plugin_id: "github",
      repo_external_id: "x/y",
      pr_id: "p1",
      pr_number: 42,
      pr_html_url: "https://x/y/pull/42",
      author_login: "alice",
      is_draft: false,
      created_at: "2026-05-23T00:00:00Z",
      updated_at: "2026-05-23T00:00:00Z",
      m06_status: "running",
      current_stage: "Review",
      findings_count: 0,
      max_severity: null,
      builder_kind: "user",
      builder_display_name: "alice",
      stages: [
        {
          name: "Review",
          state: "running",
          attempt_count: 1,
          current_attempt: 1,
          started_at: "2026-05-23T00:00:00Z",
          completed_at: null,
          workflow_execution_id: "wfx-1",
        },
      ],
      builder: { kind: "user", display_name: "alice" },
    },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useFindingsForTicket: () => ({ data: [], isLoading: false }),
  useReviewJobsForTicket: () => ({ data: [], isLoading: false }),
  useHitlHistory: () => ({ data: [], isLoading: false }),
  useAckFinding: () => ({ mutate: vi.fn(), isPending: false }),
  usePushBackFinding: () => ({ mutate: vi.fn(), isPending: false }),
  useHitlRespond: () => ({ mutate: vi.fn(), isPending: false }),
  useCancelReviewerJobs: () => ({ mutate: vi.fn(), isPending: false }),
  useRereviewMutation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { TicketDetailPage } from "../TicketDetailPage";

describe("TicketDetailPage", () => {
  it("renders the title + repo + status pill", () => {
    render(<TicketDetailPage />);
    expect(screen.getByTestId("ticket-detail")).toBeInTheDocument();
    expect(screen.getByText("Add /metrics endpoint")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-status-running")).toBeInTheDocument();
  });

  it("renders the stage indicator from ticket.stages", () => {
    render(<TicketDetailPage />);
    expect(screen.getByTestId("stage-indicator")).toBeInTheDocument();
    expect(screen.getByTestId("stage-Review")).toBeInTheDocument();
  });

  it("renders all 3 tabs in the tab strip", () => {
    render(<TicketDetailPage />);
    expect(screen.getByTestId("ticket-tab-findings")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-tab-activity")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-tab-hitl")).toBeInTheDocument();
  });

  it("exposes both Cancel and Re-run buttons when ticket is non-terminal", () => {
    render(<TicketDetailPage />);
    expect(screen.getByTestId("ticket-cancel-button")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-rerun-button")).toBeInTheDocument();
  });
});
