import type { Ticket } from "@core/api";
import { http, HttpResponse } from "msw";

export const TICKET_FIXTURE: Ticket = {
  id: "t1",
  org_id: "o1",
  source: "github_pr",
  source_external_id: "x/y#1",
  title: "Add /metrics endpoint",
  description: null,
  status: "running",
  plugin_id: "github",
  repo_external_id: "x/y",
  pr_id: "p1",
  pr_number: 42,
  pr_html_url: "https://x/y/pull/42",
  author_login: "alice",
  is_draft: false,
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
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
};

export const ticketsHandlers = [
  http.get("/api/tickets", () => {
    return HttpResponse.json({ items: [TICKET_FIXTURE], next_cursor: null });
  }),

  http.get("/api/tickets/:ticketId", ({ params }) => {
    if (params.ticketId === "t1") {
      return HttpResponse.json(TICKET_FIXTURE);
    }
    return HttpResponse.json({ error: "not found" }, { status: 404 });
  }),

  http.get("/api/tickets/:ticketId/audit", () => {
    return HttpResponse.json([]);
  }),

  http.get("/api/tickets/:ticketId/hitl/history", () => {
    return HttpResponse.json([]);
  }),

  http.post("/api/tickets/:ticketId/hitl/respond", () => {
    return HttpResponse.json({ stage: "Review", next_state: "running" });
  }),

  http.get("/api/reviewer/jobs/by-ticket/:ticketId", () => {
    return HttpResponse.json([]);
  }),

  http.get("/api/reviewer/findings/by-ticket/:ticketId", () => {
    return HttpResponse.json([]);
  }),

  http.get("/api/reviewer/conversations/by-ticket/:ticketId", () => {
    return HttpResponse.json([]);
  }),

  http.get("/api/reviewer/reviews/by-ticket/:ticketId", () => {
    return HttpResponse.json([]);
  }),

  http.get("/api/tickets/dashboard", () => {
    return HttpResponse.json({
      stats: { in_flight: 0, hitl_pending: 0, completed_today: 0, failed_today: 0 },
      in_flight: [],
      needs_attention: [],
    });
  }),

  http.post("/api/reviewer/cancel", () => {
    return HttpResponse.json({ cancelled_count: 0 });
  }),

  http.post("/api/reviewer/rereview", () => {
    return HttpResponse.json({ scheduled_count: 1 });
  }),

  http.get("/api/reviewer/metrics", () => {
    return HttpResponse.json({
      review_jobs_by_status: {},
      total_reviews_posted: 0,
      failure_count: 0,
      failure_rate: 0,
    });
  }),

  http.post("/api/reviewer/findings/:findingId/ack", ({ params }) => {
    return HttpResponse.json({ finding_id: params.findingId, state: "acknowledged" });
  }),

  http.post("/api/reviewer/findings/:findingId/push-back", ({ params }) => {
    return HttpResponse.json({ finding_id: params.findingId, state: "resolved_unverified" });
  }),
];
