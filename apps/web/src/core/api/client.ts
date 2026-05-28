/**
 * Typed API client.
 *
 * : types are hand-declared inline rather than generated, because the
 * surface is small and adding the openapi codegen pipeline is polish.
 */

import createClient from "openapi-fetch";
import { getCurrentOrgSlug } from "./org-context";

export type HealthResponse = {
  status: "ok" | "degraded";
  db_ok: boolean;
  version: string;
};

export type Ticket = {
  id: string;
  org_id: string;
  source: string;
  source_external_id: string;
  title: string;
  description: string | null;
  // collapsed status — 5-state UI vocab (the legacy 4-state values
  // were rewritten one-shot in backend migration 023).
  status: "running" | "hitl" | "done" | "failed" | "cancelled";
  plugin_id: string;
  repo_external_id: string;
  pr_id: string | null;
  // Enriched from the linked PR at read-time. Null for the brief moment
  // between ticket creation and PR row insertion.
  pr_number: number | null;
  pr_html_url: string | null;
  author_login: string | null;
  is_draft: boolean | null;
  created_at: string;
  updated_at: string;
  current_stage: string | null;
  findings_count: number;
  max_severity: "low" | "medium" | "high" | null;
  builder_kind: "user" | "system";
  builder_display_name: string | null;
  // Present on GET /api/tickets/:id. Absent on the list
  // endpoint and on cached entries that predate the extension.
  stages?: Array<{
    name: string;
    state: string;
    attempt_count: number;
    current_attempt: number;
    started_at: string | null;
    completed_at: string | null;
    workflow_execution_id: string;
  }>;
  builder?: {
    kind: "user" | "system";
    user_id?: string | null;
    display_name: string | null;
    avatar_url?: string | null;
  };
};

export type Lesson = {
  id: string;
  org_id: string;
  plugin_id: string;
  repo_external_id: string;
  title: string;
  body: string;
  source_pr_url: string | null;
  /** UUID of the user who created the lesson; null for system/reviewer-created
   * rows (workspace agent, pre-backfills). */
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

/** Per-finding snippet line — agent emits these to render a structured diff under the body. */
export type FindingSnippetLine = {
  line_number: number;
  kind: "context" | "add" | "del";
  text: string;
};

export type Finding = {
  file: string | null;
  line_start: number | null;
  line_end: number | null;
  severity: "must-fix" | "nit" | "suggestion" | "info";
  title: string;
  body: string;
  rationale: string | null;
  snippet: FindingSnippetLine[] | null;
  applied_lesson_ids: string[];
  // Which yaaos subagent surfaced this finding (e.g. "yaaos-architecture").
  source_agent: string | null;
};

/**
 * Pre-rendered activity event captured from the coding-agent stream.
 *
 * `message` is rendered by the backend so the FE doesn't interpret raw
 * Claude shapes; `detail` carries kind-specific extras for expanded views.
 */
export type ReviewJobActivityEvent = {
  ts: string;
  kind: string;
  message: string;
  detail?: Record<string, unknown> | null;
};

export type ReviewJob = {
  id: string;
  pr_id: string;
  status: string;
  skip_reason: string | null;
  scheduled_at: string;
  started_at: string | null;
  completed_at: string | null;
  last_heartbeat_at: string | null;
  current_step: string | null;
  prompt_hash: string | null;
  lessons_applied: string[] | null;
  tokens_in: number | null;
  tokens_out: number | null;
  error_message: string | null;
  duration_s: number | null;
  review_external_id: string | null;
  findings: Finding[] | null;
  // Chronological events captured from the coding-agent stream. Empty array
  // for rows from before migration 006 or runs that didn't emit anything.
  activity_log: ReviewJobActivityEvent[];
  // CLI model alias requested at kickoff (e.g. "opus"). On completion this is
  // updated to the resolved name the CLI reported (e.g. "claude-opus-4-7-...").
  model: string | null;
  effort: string | null;
};

export type AuditEntry = {
  id: string;
  org_id: string;
  entity_kind: string;
  entity_id: string;
  kind: string;
  payload: Record<string, unknown>;
  actor: { kind: string; login: string | null; agent_id: string | null };
  created_at: string;
};

type Paths = {
  "/api/health": {
    get: { responses: { 200: { content: { "application/json": HealthResponse } } } };
  };
};

const baseUrl = typeof window !== "undefined" ? window.location.origin : "http://localhost";

export const apiClient = createClient<Paths>({ baseUrl });

function _readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return m && m[1] !== undefined ? decodeURIComponent(m[1]) : null;
}

// Lightweight typed fetch for our hand-written endpoints (the openapi-fetch client
// only carries types for the small set above).
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // Inject X-Org-Slug from the current route unless the caller already set one.
  const slug = getCurrentOrgSlug();
  const callerHeaders = (init?.headers as Record<string, string>) ?? {};
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...callerHeaders,
  };
  if (slug && !("X-Org-Slug" in callerHeaders) && !("x-org-slug" in callerHeaders)) {
    headers["X-Org-Slug"] = slug;
  }
  // Double-submit CSRF: every mutating request echoes the `yaaos_csrf` cookie
  // in the `X-CSRF-Token` header. Safe methods don't need it.
  const method = (init?.method ?? "GET").toUpperCase();
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && !("X-CSRF-Token" in callerHeaders)) {
    const csrf = _readCookie("yaaos_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const r = await fetch(`${baseUrl}${path}`, {
    credentials: "include",
    ...init,
    headers,
  });
  if (r.status === 401) {
    // Auth-dead session — central handler hard-navigates to /login and
    // throws AuthError. Importing lazily breaks the import cycle: this
    // module is part of every page's load path, and auth-failure does
    // not need to be on it.
    const { handleAuthFailure } = await import("./auth-failure");
    await handleAuthFailure(r);
    // handleAuthFailure always throws — TypeScript needs the unreachable.
    throw new Error("unreachable");
  }
  if (!r.ok) {
    const body = await r.text();
    throw new Error(`${r.status} ${path}: ${body}`);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}
