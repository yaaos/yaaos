/**
 * Component tests: live-activity pane (stage-activity-live) + overview ticker.
 *
 * Stubs the global `EventSource` so there is no real network I/O and so we
 * can fire synthetic frames. Does NOT test `useRunActivityTail` in isolation —
 * that is covered by `core/sse/test/run-activity.test.ts`. Here we assert the
 * render-level contracts: the correct branch renders based on `isRunning`, and
 * the Overview ticker appears only when frames have arrived.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { TicketDetailPage } from "../public/TicketDetailPage";

// ── FakeEventSource ──────────────────────────────────────────────────────────

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  url: string;
  withCredentials: boolean;

  constructor(url: string, init?: { withCredentials?: boolean }) {
    this.url = url;
    this.withCredentials = init?.withCredentials ?? false;
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  emit(payload: object): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

function activitySources(): FakeEventSource[] {
  return FakeEventSource.instances.filter(
    (es) => !es.closed && es.url.includes("workspace_activity"),
  );
}

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as unknown as { EventSource: unknown }).EventSource =
    FakeEventSource as unknown as typeof EventSource;
});

afterEach(() => {
  FakeEventSource.instances = [];
});

// ── Fixtures ─────────────────────────────────────────────────────────────────

import { vi } from "vitest";
vi.mock("@tanstack/react-router", () => ({
  useParams: () => ({ ticketId: "t1" }),
}));

const TICKET = {
  id: "t1",
  org_id: "o1",
  source: "github_pr",
  source_external_id: "x/y#1",
  title: "Live activity test ticket",
  description: null,
  status: "in_review",
  plugin_id: "github",
  repo_external_id: "x/y",
  pr_id: "p1",
  pr_number: 1,
  pr_html_url: "https://x/y/pull/1",
  author_login: "alice",
  is_draft: false,
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
  current_stage: null,
  findings_count: 0,
  max_severity: null,
  builder_kind: "user" as const,
  builder_display_name: "alice",
  builder: { kind: "user" as const, display_name: "alice" },
};

/** A run whose first stage is `running` — drives the live pane. */
const RUNNING_RUN = {
  id: "run-1",
  pipeline_name: "dev",
  state: "running",
  kickoff: { intake_point_id: "test", actor_kind: "user", actor_login: "alice", input_text: null },
  created_at: "2026-06-01T00:00:00Z",
  completed_at: null,
  failure_reason: null,
  stages: [
    {
      id: "se-running",
      stage_index: 0,
      kind: "skill",
      stage_name: "analyze",
      status: "running",
      confidence: null,
      review_iterations: 0,
      boundary_outcome: null,
      artifact_ids: [],
      action_result: null,
      decisions: [],
      failure_reason: null,
      started_at: "2026-06-01T00:00:00Z",
      completed_at: null,
    },
  ],
};

/** A run whose first stage is `completed` — drives the persisted pane. */
const COMPLETED_RUN = {
  ...RUNNING_RUN,
  state: "completed",
  stages: [
    {
      ...RUNNING_RUN.stages[0],
      status: "completed",
      completed_at: "2026-06-01T00:01:00Z",
    },
  ],
};

const IN_FLIGHT_OVERVIEW = {
  status: "in_flight",
  run: RUNNING_RUN,
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function withBaseHandlers() {
  server.use(
    http.get("/api/tickets/t1", () => HttpResponse.json(TICKET)),
    http.get("/api/tickets/t1/audit", () => HttpResponse.json([])),
    http.get("/api/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("StageActivityBody live vs persisted branch", () => {
  it("renders stage-activity-live when the stage is running and the accordion is opened", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [RUNNING_RUN] })),
      http.get("/api/pipelines/runs/overview", () => HttpResponse.json(IN_FLIGHT_OVERVIEW)),
    );
    render(wrap(<TicketDetailPage />));

    // Switch to the Runs tab so the run card renders.
    const runsTab = await screen.findByTestId("ticket-tab-runs");
    await act(async () => {
      runsTab.click();
    });

    // Open the Activity accordion for the running stage.
    const activityBtn = await screen.findByTestId("stage-activity-toggle-analyze");
    await act(async () => {
      activityBtn.click();
    });

    // The live pane placeholder text should appear.
    expect(await screen.findByText(/Streaming live/)).toBeInTheDocument();
    // The testid sentinel should be present.
    expect(screen.getByTestId("stage-activity-live")).toBeInTheDocument();
  });

  it("renders the persisted branch (no stage-activity-live) when the stage is completed", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [COMPLETED_RUN] })),
      http.get("/api/pipelines/runs/overview", () =>
        HttpResponse.json({
          status: "terminal",
          outcome: { state: "completed", pr_url: null, failure_reason: null },
        }),
      ),
      // Stub the activity endpoint — no events.
      http.get("/api/pipelines/runs/:runId/stages/:seId/activity", () =>
        HttpResponse.json({ activity: null }),
      ),
    );
    render(wrap(<TicketDetailPage />));

    const runsTab = await screen.findByTestId("ticket-tab-runs");
    await act(async () => {
      runsTab.click();
    });

    const activityBtn = await screen.findByTestId("stage-activity-toggle-analyze");
    await act(async () => {
      activityBtn.click();
    });

    // Persisted branch placeholder should appear (no activity recorded).
    expect(await screen.findByText(/No activity recorded/)).toBeInTheDocument();
    // Live pane must NOT be present.
    expect(screen.queryByTestId("stage-activity-live")).not.toBeInTheDocument();
  });
});

describe("overview-live-ticker", () => {
  it("renders overview-live-ticker when a live activity frame arrives", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [RUNNING_RUN] })),
      http.get("/api/pipelines/runs/overview", () => HttpResponse.json(IN_FLIGHT_OVERVIEW)),
    );
    render(wrap(<TicketDetailPage />));

    // Wait for the in-flight attention block to appear.
    await screen.findByTestId("attention-block");

    // Ticker must NOT be visible yet (no frames arrived).
    expect(screen.queryByTestId("overview-live-ticker")).not.toBeInTheDocument();

    // Emit a frame on the workspace_activity EventSource.
    const es = activitySources()[0];
    if (!es) throw new Error("no workspace_activity EventSource opened");

    act(() => {
      es.emit({
        kind: "assistant_message",
        ts: "2026-06-01T00:00:05Z",
        message: "analyzing the diff",
        detail: null,
      });
    });

    // Now the ticker should appear with the message.
    const ticker = await screen.findByTestId("overview-live-ticker");
    expect(ticker).toHaveTextContent("analyzing the diff");
  });

  it("hides the ticker when no frames have arrived", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [RUNNING_RUN] })),
      http.get("/api/pipelines/runs/overview", () => HttpResponse.json(IN_FLIGHT_OVERVIEW)),
    );
    render(wrap(<TicketDetailPage />));

    await screen.findByTestId("attention-block");

    // No frames emitted — ticker must be absent.
    expect(screen.queryByTestId("overview-live-ticker")).not.toBeInTheDocument();
  });
});
