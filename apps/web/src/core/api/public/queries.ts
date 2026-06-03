import {
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { type Lesson, type ReviewJob, type Ticket, apiFetch } from "./client";

// ── Auth / session ────────────────────────────────────────────────────────────

export interface EmailSummary {
  email: string;
  is_primary: boolean;
  verified: boolean;
}

export interface MembershipSummary {
  org_id: string;
  slug: string;
  display_name: string;
  role: "owner" | "admin" | "builder";
  handle: string;
}

/** Response of `GET /api/auth/me`. */
export interface CurrentUser {
  user: {
    id: string;
    display_name: string;
    primary_email: string | null;
    emails: EmailSummary[];
  };
  memberships: MembershipSummary[];
}

/** Shared query options for `/api/auth/me`. Use `currentUserQueryOptions` to
 *  read or subscribe to this cache entry without triggering an additional fetch. */
export const currentUserQueryOptions = queryOptions<CurrentUser | null>({
  queryKey: ["auth", "me"],
  queryFn: async () => {
    try {
      return await apiFetch<CurrentUser>("/api/auth/me");
    } catch (err) {
      if ((err as Error)?.message?.startsWith("401")) return null;
      throw err;
    }
  },
  staleTime: 30_000,
});

/** Fetches `/api/auth/me`. Returns `null` (not throws) when unauthenticated. */
export function useCurrentUser() {
  return useSuspenseQuery(currentUserQueryOptions);
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch("/api/auth/logout", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth"] }),
  });
}

export function useLogoutAll() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch("/api/auth/logout-all", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth"] }),
  });
}

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
  return useSuspenseQuery<MineOrg[]>({
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
  return useSuspenseQuery<DashboardResponse>({
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
  return useSuspenseQuery<AgentRow[]>({
    queryKey: ["agents", orgSlug],
    queryFn: () =>
      orgSlug
        ? apiFetch<AgentRow[]>(`/api/orgs/${encodeURIComponent(orgSlug)}/agents`)
        : Promise.resolve([]),
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
  return useSuspenseQuery<Notification[]>({
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
  return useSuspenseQuery<Ticket[]>({
    queryKey: ["tickets"],
    queryFn: async () => {
      const resp = await apiFetch<TicketsListResponse>("/api/tickets");
      return resp.items;
    },
  });
}

/**
 * @param ticket_id must be non-empty; enforced by the required route URL param.
 */
export function useTicket(ticket_id: string) {
  return useSuspenseQuery<Ticket>({
    queryKey: ["tickets", ticket_id],
    queryFn: () => apiFetch<Ticket>(`/api/tickets/${ticket_id}`),
  });
}

/**
 * @param ticket_id must be non-empty; enforced by the required route URL param.
 */
export function useReviewJobsForTicket(ticket_id: string) {
  return useSuspenseQuery<ReviewJob[]>({
    queryKey: ["reviewer", "jobs", ticket_id],
    queryFn: () => apiFetch<ReviewJob[]>(`/api/reviewer/jobs/by-ticket/${ticket_id}`),
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

/**
 * @param ticket_id must be non-empty; enforced by the required route URL param.
 */
export function useFindingsForTicket(ticket_id: string, includeTerminal = false) {
  return useSuspenseQuery<FindingRow[]>({
    queryKey: ["reviewer", "findings", ticket_id, includeTerminal],
    queryFn: () =>
      apiFetch<FindingRow[]>(
        `/api/reviewer/findings/by-ticket/${ticket_id}${
          includeTerminal ? "?include_terminal=true" : ""
        }`,
      ),
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

/**
 * @param ticket_id must be non-empty; enforced by the required route URL param.
 */
export function useHitlHistory(ticket_id: string) {
  return useSuspenseQuery<HitlHistoryEntry[]>({
    queryKey: ["tickets", ticket_id, "hitl-history"],
    queryFn: () => apiFetch<HitlHistoryEntry[]>(`/api/tickets/${ticket_id}/hitl/history`),
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

export function useLessons(filter: LessonsFilter = {}) {
  const params = new URLSearchParams();
  for (const r of filter.repos ?? []) params.append("repo_external_id", r);
  if (filter.q) params.set("q", filter.q);
  if (filter.created_by) params.set("created_by", filter.created_by);
  if (filter.sort) params.set("sort", filter.sort);
  if (filter.limit) params.set("limit", String(filter.limit));
  const qs = params.toString();
  return useSuspenseQuery<Lesson[]>({
    queryKey: [
      "lessons",
      filter.repos ?? "all",
      filter.q ?? "",
      filter.created_by ?? "",
      filter.sort ?? "",
    ],
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

export type GithubInstallation = {
  app_configured: boolean;
  installed: boolean;
  slug: string | null;
  account_login: string | null;
  install_external_id: string | null;
  installed_at: string | null;
  installations_url: string | null;
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

export function useGithubRepositories() {
  return useSuspenseQuery<GithubRepositoriesResponse>({
    queryKey: ["github", "repositories"],
    queryFn: () => apiFetch<GithubRepositoriesResponse>("/api/github/repositories"),
    refetchInterval: 30_000,
  });
}

export function useGithubInstallation() {
  return useSuspenseQuery<GithubInstallation>({
    queryKey: ["github", "installation"],
    queryFn: () => apiFetch<GithubInstallation>("/api/github/installation"),
    refetchInterval: 10_000,
  });
}

// ── Plugin discovery ─────────────────────────────────────────────────────────

interface ListAvailableResponse {
  plugins: PluginMeta[];
}

/** GET /api/plugins/available?type=... */
export function useAvailablePlugins(type: "vcs" | "coding_agent") {
  return useSuspenseQuery<PluginMeta[]>({
    queryKey: ["plugins", "available", type],
    queryFn: async () => {
      const body = await apiFetch<ListAvailableResponse>(
        `/api/plugins/available?type=${encodeURIComponent(type)}`,
      );
      return body.plugins;
    },
    staleTime: 60_000,
  });
}
