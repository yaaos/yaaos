import type { Ticket } from "@core/api/public/client";
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
  current_stage: "pr_review_v1",
  findings_count: 0,
  max_severity: null,
  builder_kind: "user",
  builder_display_name: "alice",
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

  http.get("/api/tickets/:ticketId/workflow-runs", () => {
    return HttpResponse.json([]);
  }),

  http.get("/api/tickets/:ticketId/audit", () => {
    return HttpResponse.json([]);
  }),

  http.get("/api/tickets/:ticketId/hitl/history", () => {
    return HttpResponse.json([]);
  }),

  http.post("/api/tickets/:ticketId/hitl/respond", () => {
    return HttpResponse.json({ stage: "pr_review_v1", next_state: "running" });
  }),

  http.get("/api/reviewer/findings/by-ticket/:ticketId", () => {
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

  http.get("/api/reviewer/metrics", () => {
    return HttpResponse.json({
      review_jobs_by_status: {},
      total_reviews_posted: 0,
      failure_count: 0,
      failure_rate: 0,
    });
  }),
];
