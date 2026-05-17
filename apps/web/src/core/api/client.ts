/**
 * Typed API client.
 *
 * M01: types are hand-declared inline rather than generated, because the
 * surface is small and adding the openapi codegen pipeline is M02+ polish.
 */

import createClient from "openapi-fetch";

export type HealthResponse = {
  status: "ok" | "degraded";
  db_ok: boolean;
  version: string;
};

export type OnboardingStatus = {
  github_app_installed: boolean;
  anthropic_key_set: boolean;
};

export type Ticket = {
  id: string;
  org_id: string;
  source: string;
  source_external_id: string;
  title: string;
  description: string | null;
  status: "open" | "in_review" | "complete" | "abandoned";
  plugin_id: string;
  repo_external_id: string;
  pr_id: string | null;
  // Enriched from the linked PR at read-time. Null for the brief moment
  // between ticket creation and PR row insertion.
  pr_number: number | null;
  author_login: string | null;
  is_draft: boolean | null;
  created_at: string;
  updated_at: string;
};

export type Lesson = {
  id: string;
  org_id: string;
  plugin_id: string;
  repo_external_id: string;
  title: string;
  body: string;
  source_pr_url: string | null;
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

// Lightweight typed fetch for our hand-written endpoints (the openapi-fetch client
// only carries types for the small set above).
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${baseUrl}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) {
    const body = await r.text();
    throw new Error(`${r.status} ${path}: ${body}`);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}
