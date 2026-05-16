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

export type ReviewerAgent = {
  id: string;
  org_id: string;
  name: string;
  prompt_text: string;
  coding_agent_plugin_id: string;
  is_built_in: boolean;
};

export type ReviewJob = {
  id: string;
  pr_id: string;
  agent_id: string;
  kind: string;
  status: string;
  skip_reason: string | null;
  scheduled_at: string;
  started_at: string | null;
  completed_at: string | null;
  prompt_hash: string | null;
  lessons_applied: string[] | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  duration_s: number | null;
  review_external_id: string | null;
  findings: unknown[] | null;
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
