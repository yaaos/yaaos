import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type AuditEntry,
  type HealthResponse,
  type Lesson,
  type OnboardingStatus,
  type ReviewJob,
  type Ticket,
  apiClient,
  apiFetch,
} from "./client";

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

export function useOnboarding() {
  return useQuery<OnboardingStatus>({
    queryKey: ["onboarding"],
    queryFn: () => apiFetch<OnboardingStatus>("/api/settings/onboarding"),
    refetchInterval: 5_000,
  });
}

export function useTickets() {
  return useQuery<Ticket[]>({
    queryKey: ["tickets"],
    queryFn: () => apiFetch<Ticket[]>("/api/tickets"),
    refetchInterval: 3_000,
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
    queryFn: () => apiFetch<FindingThread>(`/api/reviewer/threads/by-finding/${finding_id}`),
    enabled: !!finding_id,
    refetchInterval: 5_000,
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

export function useLessons(repo_external_id?: string) {
  return useQuery<Lesson[]>({
    queryKey: ["memory", repo_external_id ?? "all"],
    queryFn: () =>
      apiFetch<Lesson[]>(
        `/api/memory${repo_external_id ? `?repo_external_id=${encodeURIComponent(repo_external_id)}` : ""}`,
      ),
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
      apiFetch<Lesson>("/api/memory", {
        method: "POST",
        body: JSON.stringify(l),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory"] }),
  });
}

export function useDeleteLesson() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ status: string }>(`/api/memory/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory"] }),
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
  credentials_configured: boolean;
  installed: boolean;
  app_id: string | null;
  slug: string | null;
  account_login: string | null;
  install_external_id: string | null;
  installed_at: string | null;
  install_url: string | null;
  installations_url: string | null;
};

export type SetGithubCredentialsInput = {
  app_id: string;
  slug: string;
  private_key: string;
  webhook_secret: string;
};

export function useSetGithubCredentials() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: SetGithubCredentialsInput) =>
      apiFetch<{ status: string }>("/api/github/credentials", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["github", "installation"] });
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["plugin-health", "github"] });
    },
  });
}

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

export function usePluginsList() {
  return useQuery<PluginMeta[]>({
    queryKey: ["plugins"],
    queryFn: () => apiFetch<PluginMeta[]>("/api/settings/plugins"),
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

// ── Org settings (workspace_provider + registered_iam_arn) ──────────────

export type WorkspaceProvider = "in_memory" | "remote_agent";

export type OrgSettings = {
  slug: string;
  session_timeout_override: number | null;
  workspace_provider: WorkspaceProvider | null;
  registered_iam_arn: string | null;
};

export function useOrgSettings() {
  return useQuery<OrgSettings>({
    queryKey: ["org-settings"],
    queryFn: () => apiFetch<OrgSettings>("/api/orgs"),
  });
}

export type UpdateOrgSettingsInput = {
  workspace_provider?: WorkspaceProvider | null;
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
