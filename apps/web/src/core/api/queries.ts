import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type AuditEntry,
  type HealthResponse,
  type Lesson,
  type ReviewJob,
  type Ticket,
  apiClient,
  apiFetch,
} from "./client";

// ── Broken-creds summary ─────────────────────────────────────────────────────

export interface BrokenIntegrationSummary {
  provider: string;
}

export interface BrokenSummaryOrg {
  org_id: string;
  broken_integrations: BrokenIntegrationSummary[];
}

export interface BrokenSummaryResponse {
  orgs: BrokenSummaryOrg[];
}

/** Cross-org broken-credentials summary for the cookie-bearer.
 *  Owners + Admins get non-empty lists; Builders always see empty. */
export function useBrokenSummary() {
  return useQuery<BrokenSummaryResponse | null>({
    queryKey: ["integrations", "broken-summary"],
    queryFn: async () => {
      try {
        return await apiFetch<BrokenSummaryResponse>("/api/integrations/broken-summary");
      } catch (err) {
        if ((err as Error)?.message?.startsWith("401")) return null;
        throw err;
      }
    },
    staleTime: 30_000,
  });
}

export function useHealth() {
  return useQuery<HealthResponse>({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/api/health");
      if (error) throw new Error("health check failed");
      if (!data) throw new Error("no data");
      return data;
    },
    refetchInterval: 5_000,
  });
}

/** Aggregated readiness for the "not configured" gate. */
export interface ConfigStatus {
  configured: boolean;
  missing: Array<"vcs" | "coding_agent" | "api_key" | "workspace">;
  admins: Array<{ user_id: string; display_name: string; primary_email: string | null }>;
}

export function useConfigStatus() {
  return useQuery<ConfigStatus>({
    queryKey: ["config-status"],
    queryFn: () => apiFetch<ConfigStatus>("/api/orgs/config-status"),
    refetchInterval: 30_000,
  });
}

/** Cross-org list of the cookie-bearer's memberships. Powers the org switcher
 *  chip + the `/orgs` picker page. */
export interface MineOrg {
  id: string;
  slug: string;
  name: string;
  role: "owner" | "admin" | "builder";
  last_used_at: string | null;
}

export function useMyOrgs() {
  return useQuery<MineOrg[]>({
    queryKey: ["orgs", "mine"],
    queryFn: () => apiFetch<MineOrg[]>("/api/orgs/mine"),
  });
}

/** — Login page provider button (E2a.18). Returns the SSO IdP
 *  matching the email's domain, or github fallback. Today the backend
 *  always returns `provider: "github"` (D8.1). */
export interface SsoDiscoverResult {
  provider: "github" | "saml";
  saml_org_slug?: string;
  saml_idp_name?: string;
}

export function useSsoDiscover() {
  return useMutation({
    mutationFn: (email: string) =>
      apiFetch<SsoDiscoverResult>(`/api/sso/discover?email=${encodeURIComponent(email)}`),
  });
}

/** — Org-picker "Create org" modal (E2a.19). On success the
 *  caller is Admin of the new org; the SPA navigates into it. */
export interface CreateOrgResponse {
  id: string;
  slug: string;
  name: string;
  role: "admin";
}

export function useCreateOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { name: string; slug: string }) =>
      apiFetch<CreateOrgResponse>("/api/orgs", {
        method: "POST",
        body: JSON.stringify(args),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orgs", "mine"] });
    },
  });
}

/** Single-round-trip Dashboard projection per E2a.3. */
export interface DashboardStats {
  in_flight: number;
  hitl_pending: number;
  completed_today: number;
  failed_today: number;
}

export interface DashboardResponse {
  stats: DashboardStats;
  in_flight: Ticket[];
  needs_attention: Ticket[];
}

export function useDashboard() {
  return useQuery<DashboardResponse>({
    queryKey: ["tickets", "dashboard"],
    queryFn: () => apiFetch<DashboardResponse>("/api/tickets/dashboard"),
  });
}

/** Per-org workspace agents within the 1-hour retention window.
 *  Invalidated live via `agent_liveness_changed` SSE; no polling. */
export interface AgentRow {
  id: string;
  instance_id: string;
  state: "reachable" | "stale" | "offline";
  last_heartbeat_at: string | null;
  os: string | null;
  cpu_count: number | null;
  memory_bytes: number | null;
  claimed_workspace_count: number;
  version: string | null;
}

export function useAgents(orgSlug: string) {
  return useQuery<AgentRow[]>({
    queryKey: ["agents"],
    queryFn: () => apiFetch<AgentRow[]>(`/api/orgs/${encodeURIComponent(orgSlug)}/agents`),
    enabled: !!orgSlug,
  });
}

/** Cross-org notifications for the cookie-bearer. Per E2a.6 + E2a.7. */
export interface Notification {
  id: string;
  user_id: string;
  org_id: string;
  type: string;
  ticket_id: string | null;
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
}

export interface NotificationsPopover {
  items: Notification[];
  unread_count: number;
}

export function useNotifications(readState: "all" | "unread" | "read" = "all") {
  return useQuery<Notification[]>({
    queryKey: ["notifications", readState],
    queryFn: () => apiFetch<Notification[]>(`/api/notifications?read_state=${readState}`),
    refetchInterval: 30_000,
  });
}

export function useNotificationsPopover() {
  return useQuery<NotificationsPopover>({
    queryKey: ["notifications", "popover"],
    queryFn: () => apiFetch<NotificationsPopover>("/api/notifications/popover"),
    refetchInterval: 30_000,
  });
}

export function useMarkNotificationRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<Notification>(`/api/notifications/${id}/read`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export function useMarkAllNotificationsRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<{ marked: number }>("/api/notifications/mark-read", {
        method: "POST",
        body: JSON.stringify({}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

interface TicketsListResponse {
  items: Ticket[];
  next_cursor: string | null;
}

export function useTickets() {
  return useQuery<Ticket[]>({
    queryKey: ["tickets"],
    queryFn: async () => {
      const resp = await apiFetch<TicketsListResponse>("/api/tickets");
      return resp.items;
    },
  });
}

export function useTicket(ticket_id: string) {
  return useQuery<Ticket>({
    queryKey: ["tickets", ticket_id],
    queryFn: () => apiFetch<Ticket>(`/api/tickets/${ticket_id}`),
    enabled: !!ticket_id,
  });
}

export function useTicketAudit(ticket_id: string) {
  return useQuery<AuditEntry[]>({
    queryKey: ["tickets", ticket_id, "audit"],
    queryFn: () => apiFetch<AuditEntry[]>(`/api/tickets/${ticket_id}/audit`),
    enabled: !!ticket_id,
    refetchInterval: 3_000,
  });
}

export function useReviewJobsForTicket(ticket_id: string) {
  return useQuery<ReviewJob[]>({
    queryKey: ["reviewer", "jobs", ticket_id],
    queryFn: () => apiFetch<ReviewJob[]>(`/api/reviewer/jobs/by-ticket/${ticket_id}`),
    enabled: !!ticket_id,
    refetchInterval: 3_000,
  });
}

/**
 * Durable-findings list (plan/notes/full-pr-flow.md §9). Open + acknowledged
 * by default; pass include_terminal to also fetch resolved + stale.
 */
export interface FindingRow {
  id: string;
  state: "open" | "acknowledged" | "resolved_confirmed" | "resolved_unverified" | "stale";
  severity: "blocker" | "major" | "minor" | "nit";
  rule_id: string;
  title: string;
  body: string;
  rationale: string;
  confidence: number;
  first_seen_review_id: string;
  last_observed_review_id: string;
  file_path: string;
  line_start: number;
  line_end: number;
}

export function useFindingsForTicket(ticket_id: string, includeTerminal = false) {
  return useQuery<FindingRow[]>({
    queryKey: ["reviewer", "findings", ticket_id, includeTerminal],
    queryFn: () =>
      apiFetch<FindingRow[]>(
        `/api/reviewer/findings/by-ticket/${ticket_id}${
          includeTerminal ? "?include_terminal=true" : ""
        }`,
      ),
    enabled: !!ticket_id,
    refetchInterval: 5_000,
  });
}

/**
 * All-Conversations cross-cut (plan §9.3). Findings with ≥1 dev reply OR open
 * findings first raised before the latest review. Terminal states excluded.
 */
export interface ConversationRow {
  finding_id: string;
  state: "open" | "acknowledged";
  severity: "blocker" | "major" | "minor" | "nit";
  title: string;
  first_seen_review_id: string;
  last_message_preview: string;
  reply_count: number;
}

export function useConversationsForTicket(ticket_id: string) {
  return useQuery<ConversationRow[]>({
    queryKey: ["reviewer", "conversations", ticket_id],
    queryFn: () =>
      apiFetch<ConversationRow[]>(`/api/reviewer/conversations/by-ticket/${ticket_id}`),
    enabled: !!ticket_id,
    refetchInterval: 5_000,
  });
}

/**
 * Per-review timeline metadata (plan §9.2). One row per Review row, newest
 * first. Frontend renders each as a collapsible <details> section with
 * sequence_number / trigger_reason / scope as the header.
 */
export interface ReviewTimelineRow {
  id: string;
  sequence_number: number;
  trigger_reason: string;
  scope_kind: "full" | "incremental";
  scope_prev_sha: string | null;
  commit_sha_at_start: string | null;
  status: string;
  scheduled_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  model: string | null;
  effort: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  duration_s: number | null;
}

export function useReviewsForTicket(ticket_id: string) {
  return useQuery<ReviewTimelineRow[]>({
    queryKey: ["reviewer", "reviews", ticket_id],
    queryFn: () => apiFetch<ReviewTimelineRow[]>(`/api/reviewer/reviews/by-ticket/${ticket_id}`),
    enabled: !!ticket_id,
    refetchInterval: 5_000,
  });
}

/**
 * Thread messages + ack banner for one finding (plan §9.4).
 */
export interface ThreadMessage {
  id: string;
  author_kind: "yaaos" | "human";
  author_external_id: string;
  external_comment_id: string;
  body: string;
  classified_intent: string | null;
  created_at: string | null;
}

export interface FindingThread {
  finding_id: string;
  state: string;
  title: string;
  thread_id: string | null;
  external_thread_id: string | null;
  acknowledgment: {
    kind: string;
    rationale: string;
    made_by_external_id: string;
    created_at: string | null;
  } | null;
  messages: ThreadMessage[];
}

export function useThreadForFinding(finding_id: string | null) {
  return useQuery<FindingThread>({
    queryKey: ["reviewer", "thread", finding_id],
    queryFn: () => apiFetch<FindingThread>(`/api/reviewer/findings/${finding_id}/thread`),
    enabled: !!finding_id,
    refetchInterval: 5_000,
  });
}

/** : Builder confirms a finding ("yeah I'll fix this"). */
export function useAckFinding(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (finding_id: string) =>
      apiFetch<{ finding_id: string; state: string }>(`/api/reviewer/findings/${finding_id}/ack`, {
        method: "POST",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["reviewer", "findings", ticket_id] });
    },
  });
}

/** : Builder rejects a finding with a reason. */
export function usePushBackFinding(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { finding_id: string; reason: string }) =>
      apiFetch<{ finding_id: string; state: string }>(
        `/api/reviewer/findings/${args.finding_id}/push-back`,
        { method: "POST", body: JSON.stringify({ reason: args.reason }) },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["reviewer", "findings", ticket_id] });
    },
  });
}

/** : past HITL exchanges on a ticket — prompt + resolution + timestamps. */
export interface HitlHistoryEntry {
  id: string;
  workflow_execution_id: string;
  question_payload: Record<string, unknown>;
  resolution_payload: Record<string, unknown> | null;
  resolved_at: string | null;
  created_at: string;
}

export function useHitlHistory(ticket_id: string) {
  return useQuery<HitlHistoryEntry[]>({
    queryKey: ["tickets", ticket_id, "hitl-history"],
    queryFn: () => apiFetch<HitlHistoryEntry[]>(`/api/tickets/${ticket_id}/hitl/history`),
    enabled: !!ticket_id,
  });
}

/** : submit a HITL response. The body shape is prompt-discriminated;
 *  callers pass the dict the HITL renderer produced. */
export function useHitlRespond(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (response: Record<string, unknown>) =>
      apiFetch<{ stage: string; next_state: string }>(`/api/tickets/${ticket_id}/hitl/respond`, {
        method: "POST",
        body: JSON.stringify(response),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tickets", ticket_id] });
      qc.invalidateQueries({ queryKey: ["tickets", ticket_id, "hitl-history"] });
    },
  });
}

/**
 * `@yaaos full review` from the UI — schedules a full review.
 * Reuses the existing /api/reviewer/rereview endpoint (trigger_reason="ui_button"
 * which schedule_review treats as a full run today; the user-facing semantic
 * is "full re-review").
 */
export function useFullRereviewMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ticket_id: string) =>
      apiFetch<{ scheduled_count: number }>("/api/reviewer/rereview", {
        method: "POST",
        body: JSON.stringify({ ticket_id }),
      }),
    onSuccess: (_, ticket_id) => {
      qc.invalidateQueries({ queryKey: ["reviewer", "jobs", ticket_id] });
      qc.invalidateQueries({ queryKey: ["reviewer", "reviews", ticket_id] });
      qc.invalidateQueries({ queryKey: ["reviewer", "findings", ticket_id] });
    },
  });
}

export function useRereviewMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ticket_id: string) =>
      apiFetch<{ scheduled_count: number }>("/api/reviewer/rereview", {
        method: "POST",
        body: JSON.stringify({ ticket_id }),
      }),
    onSuccess: (_, ticket_id) => {
      qc.invalidateQueries({ queryKey: ["reviewer", "jobs", ticket_id] });
      qc.invalidateQueries({ queryKey: ["tickets", ticket_id, "audit"] });
    },
  });
}

export function useCancelReviewerJobs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ticket_id: string) =>
      apiFetch<{ cancelled_count: number }>(
        `/api/reviewer/cancel?ticket_id=${encodeURIComponent(ticket_id)}`,
        { method: "POST" },
      ),
    onSuccess: (_, ticket_id) => {
      qc.invalidateQueries({ queryKey: ["reviewer", "jobs", ticket_id] });
      qc.invalidateQueries({ queryKey: ["tickets", ticket_id, "audit"] });
    },
  });
}

export interface LessonsFilter {
  /** Repo full-names; when present each is sent as a separate
   * `repo_external_id=…` query parameter. */
  repos?: string[];
  /** Case-insensitive title+body substring. */
  q?: string;
  /** UUID of the user who created the lesson. */
  created_by?: string;
  sort?: "created_desc" | "created_asc" | "updated_desc";
  limit?: number;
}

export function useLessons(filter: LessonsFilter | string = {}) {
  // Back-compat: old callers passed `repo_external_id?: string`.
  const f: LessonsFilter =
    typeof filter === "string" ? { repos: filter ? [filter] : undefined } : filter;
  const params = new URLSearchParams();
  for (const r of f.repos ?? []) params.append("repo_external_id", r);
  if (f.q) params.set("q", f.q);
  if (f.created_by) params.set("created_by", f.created_by);
  if (f.sort) params.set("sort", f.sort);
  if (f.limit) params.set("limit", String(f.limit));
  const qs = params.toString();
  return useQuery<Lesson[]>({
    queryKey: ["lessons", f.repos ?? "all", f.q ?? "", f.created_by ?? "", f.sort ?? ""],
    queryFn: () => apiFetch<Lesson[]>(`/api/lessons${qs ? `?${qs}` : ""}`),
  });
}

export function useCreateLesson() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (l: {
      repo_external_id: string;
      title: string;
      body: string;
      source_pr_url?: string | null;
    }) =>
      apiFetch<Lesson>("/api/lessons", {
        method: "POST",
        body: JSON.stringify(l),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["lessons"] }),
  });
}

export function useDeleteLesson() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ status: string }>(`/api/lessons/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["lessons"] }),
  });
}

export function useMetricsSummary() {
  return useQuery<{
    review_jobs_by_status: Record<string, number>;
    total_reviews_posted: number;
    failure_count: number;
    failure_rate: number;
  }>({
    queryKey: ["reviewer", "metrics"],
    queryFn: () => apiFetch("/api/reviewer/metrics"),
    refetchInterval: 5_000,
  });
}

export function useSetAnthropicKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (api_key: string) =>
      apiFetch<{ status: string }>("/api/claude_code/api_key", {
        method: "POST",
        body: JSON.stringify({ api_key }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["plugin-health", "claude_code"] });
    },
  });
}

export type GithubInstallation = {
  app_configured: boolean;
  installed: boolean;
  slug: string | null;
  account_login: string | null;
  install_external_id: string | null;
  installed_at: string | null;
  installations_url: string | null;
};

export type PluginHealth = {
  healthy: boolean;
  message: string;
  checked_at: string;
};

export type PluginType = "vcs" | "coding_agent" | "workspace";

export type PluginMeta = {
  id: string;
  type: PluginType;
  display_name: string;
  description: string | null;
  docs_url: string | null;
};

export type GithubRepository = {
  full_name: string;
  html_url: string;
  private: boolean;
};

export type GithubRepositoriesResponse = {
  total_count: number;
  repositories: GithubRepository[];
  error?: string;
};

export function useGithubRepositories(enabled = true) {
  return useQuery<GithubRepositoriesResponse>({
    queryKey: ["github", "repositories"],
    queryFn: () => apiFetch<GithubRepositoriesResponse>("/api/github/repositories"),
    enabled,
    refetchInterval: 30_000,
  });
}

export function useGithubInstallation() {
  return useQuery<GithubInstallation>({
    queryKey: ["github", "installation"],
    queryFn: () => apiFetch<GithubInstallation>("/api/github/installation"),
    refetchInterval: 10_000,
  });
}

export function usePluginHealth(pluginId: string) {
  return useQuery<PluginHealth>({
    queryKey: ["plugin-health", pluginId],
    queryFn: () => apiFetch<PluginHealth>(`/api/${pluginId}/health`),
    refetchInterval: 10_000,
  });
}

// ── Org settings (registered_iam_arn + session timeout) ─────────────────

export type OrgSettings = {
  slug: string;
  session_timeout_override: number | null;
  registered_iam_arn: string | null;
};

export function useOrgSettings() {
  return useQuery<OrgSettings>({
    queryKey: ["org-settings"],
    queryFn: () => apiFetch<OrgSettings>("/api/orgs"),
  });
}

export type UpdateOrgSettingsInput = {
  registered_iam_arn?: string | null;
};

export function useUpdateOrgSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: UpdateOrgSettingsInput) =>
      apiFetch<OrgSettings>("/api/orgs", {
        method: "PATCH",
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["org-settings"] });
      qc.invalidateQueries({ queryKey: ["workspace-connection-status"] });
    },
  });
}

// ── Workspace connection status (heartbeat banner) ──────────────────────

export type WorkspaceConnectionState = "connected" | "lost" | "not_configured";

export type WorkspaceConnectionStatus = {
  state: WorkspaceConnectionState;
  pod_count: number;
  latest_heartbeat_at: string | null;
};

export function useWorkspaceConnectionStatus(enabled = true) {
  return useQuery<WorkspaceConnectionStatus>({
    queryKey: ["workspace-connection-status"],
    queryFn: () => apiFetch<WorkspaceConnectionStatus>("/api/workspaces/connection_status"),
    refetchInterval: 3_000,
    enabled,
  });
}
