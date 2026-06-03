/**
 * Typed API client.
 *
 * Hand-declared types are kept only for endpoints whose OpenAPI schema is
 * `unknown` or where the generated shape is too loose to serve the UI safely
 * (see individual type comments). Everything else is re-exported from
 * `core/api/generated/schema.d.ts`.
 */

import createClient from "openapi-fetch";
import type { components, paths } from "./generated/schema";
import { getCurrentOrgSlug } from "./org-context";

// ── Generated type aliases ─────────────────────────────────────────────────
// Simpler names for consumer import. Provenance: generated/schema.d.ts.

export type HealthResponse = components["schemas"]["HealthResponse"];

/** Lesson from backend schema. */
export type Lesson = components["schemas"]["Lesson"];

/** AuditEntry projected from `AuditEntryView` in backend schema. */
export type AuditEntry = components["schemas"]["AuditEntryView"];

// ── ReviewJob — generated base with typed activity_log overlay ─────────────
// The backend schema types `activity_log` as `{[key: string]: unknown}[]`
// because the field is a raw JSONB column with no Pydantic model. The UI
// needs a typed overlay so ActivityEventRow and the stream-merge logic
// compile safely. We keep `ReviewJobActivityEvent` as a client-side type and
// overlay it here.

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

type _GeneratedReviewJob = components["schemas"]["ReviewJob"];

/** ReviewJob with a typed `activity_log`. */
export type ReviewJob = Omit<_GeneratedReviewJob, "activity_log"> & {
  // The backend emits ReviewJobActivityEvent objects; JSONB means the spec
  // only knows the column as arbitrary object arrays. Overlay the concrete
  // type here so UI code doesn't cast at every call site.
  activity_log: ReviewJobActivityEvent[];
};

// ── Hand-typed shapes — no generated equivalent ───────────────────────────
// Endpoints below return `{[key: string]: unknown}` in the spec because the
// backend has no `response_model`. The hand types stay until the backend adds
// one; each has a comment pointing at the missing spec annotation.

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

/** Per-finding snippet line — agent emits these to render a structured diff under the body. */
export type FindingSnippetLine = {
  line_number: number;
  kind: "context" | "add" | "del";
  text: string;
};

// TODO(backend): `GET /api/reviewer/findings/by-ticket/{ticket_id}` returns
// `{[key: string]: unknown}` — needs `response_model=list[FindingView]` in
// backend/app/domain/reviewer/web.py before this type can be generated.
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

// ── openapi-fetch client ──────────────────────────────────────────────────

const baseUrl = typeof window !== "undefined" ? window.location.origin : "http://localhost";

export const apiClient = createClient<paths>({ baseUrl });

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
