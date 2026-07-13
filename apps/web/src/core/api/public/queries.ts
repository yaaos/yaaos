import {
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { toast } from "sonner";
import type { components } from "../generated/schema";
import { type Lesson, type StageActivityResponse, type Ticket, apiFetch } from "./client";

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

/** Per-org workspace agents within the 1-hour retention window.
 *  Invalidated live via `agent_changed` SSE; no polling. */
export interface AgentRow {
  id: string;
  instance_id: string;
  state: "reachable" | "stale" | "offline";
  lifecycle: "unconfigured" | "active" | "draining" | "shutdown";
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

// ── Agent bulk-action mutations ───────────────────────────────────────────────
// Both aliases below are derived from the backend OpenAPI schema — edit the
// backend response models (apps/backend/app/domain/orgs/org_settings_web.py
// + apps/backend/app/core/agent_gateway/service.py) and regenerate via
// `apps/backend/bin/dump_web_openapi` + `apps/web/bin/gen-api-types`. The
// per-row outcome string literals are reachable via
// `ShutdownResult["results"][number]["outcome"]`.

export type ShutdownResult = components["schemas"]["_BulkShutdownResponse"];
export type CancelShutdownResult = components["schemas"]["_BulkCancelShutdownResponse"];

/** Compute the toast message for a bulk-shutdown response. Exported for unit testing. */
export function shutdownToastMessage(results: ShutdownResult["results"]): string {
  const M = results.length;
  const successCount = results.filter((r) => r.outcome === "draining").length;
  const noOpCount = M - successCount;
  if (noOpCount === 0) {
    return `Shut down ${M} ${M === 1 ? "agent" : "agents"}.`;
  }
  if (successCount === 0) {
    return "No agents were shut down — all were already draining, shut down, or not found.";
  }
  return `Shut down ${successCount} of ${M} agents; ${noOpCount} were already draining, shut down, or not found.`;
}

/** Compute the toast message for a bulk cancel-shutdown response. Exported for unit testing. */
export function cancelShutdownToastMessage(results: CancelShutdownResult["results"]): string {
  const M = results.length;
  const successCount = results.filter((r) => r.outcome === "active").length;
  const noOpCount = M - successCount;
  const alreadyShutdownCount = results.filter((r) => r.outcome === "already_shutdown").length;
  if (noOpCount === 0) {
    return `Canceled shutdown for ${M} ${M === 1 ? "agent" : "agents"}.`;
  }
  if (successCount === 0) {
    let msg = "No agents were canceled — already shut down or not draining.";
    if (alreadyShutdownCount >= M / 2) {
      msg += " Restart the deployment to bring shut-down agents back.";
    }
    return msg;
  }
  return `Canceled shutdown for ${successCount} of ${M} agents; ${noOpCount} were not draining, already shut down, or not found.`;
}

/** Bulk-shutdown: set all selected active agents to lifecycle=draining. */
export function useShutdownAgents(orgSlug: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { agent_ids: string[] }) =>
      apiFetch<ShutdownResult>(`/api/orgs/${encodeURIComponent(orgSlug)}/agents/shutdown`, {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["agents", orgSlug] });
      toast(shutdownToastMessage(data.results));
    },
    onError: (err: Error) => {
      toast.error(err.message);
    },
  });
}

/** Bulk cancel-shutdown: restore selected draining agents to lifecycle=active. */
export function useCancelShutdownAgents(orgSlug: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { agent_ids: string[] }) =>
      apiFetch<CancelShutdownResult>(
        `/api/orgs/${encodeURIComponent(orgSlug)}/agents/cancel-shutdown`,
        { method: "POST", body: JSON.stringify(vars) },
      ),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["agents", orgSlug] });
      toast(cancelShutdownToastMessage(data.results));
    },
    onError: (err: Error) => {
      toast.error(err.message);
    },
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

// ── Pipeline runs (Overview / Runs tabs) ─────────────────────────────────────

export type PipelineRunView = components["schemas"]["PipelineRun"];
export type StageExecutionView = components["schemas"]["StageExecution"];
export type RunOverviewView = components["schemas"]["RunOverview"];
export type PauseDetailView = components["schemas"]["PauseDetail"];
export type RunOutcomeView = components["schemas"]["RunOutcome"];

export interface PauseResolutionBody {
  action: "approve" | "instruct" | "send_back" | "kill";
  instruction?: string | null;
  send_back_to_stage?: string | null;
}

/** All runs for the ticket, newest-first, with their stage-execution lists. */
export function useRuns(ticket_id: string) {
  return useSuspenseQuery<PipelineRunView[]>({
    queryKey: ["runs", ticket_id],
    queryFn: async () => {
      const resp = await apiFetch<{ runs: PipelineRunView[] }>(
        `/api/pipelines/runs?ticket_id=${ticket_id}`,
      );
      return resp.runs;
    },
  });
}

/**
 * Server-computed Overview-tab payload for the ticket's current run.
 * `null` on 404 — the ticket has no run yet, a legitimate empty state (not
 * an error to throw across a Suspense boundary).
 */
export function useRunOverview(ticket_id: string) {
  return useQuery<RunOverviewView | null>({
    queryKey: ["runs", "overview", ticket_id],
    queryFn: async () => {
      try {
        return await apiFetch<RunOverviewView>(
          `/api/pipelines/runs/overview?ticket_id=${ticket_id}`,
        );
      } catch (err) {
        if ((err as Error)?.message?.startsWith("404")) return null;
        throw err;
      }
    },
  });
}

/** Fetches the persisted coding-agent activity blob for one stage execution. */
export function useStageActivity(run_id: string, stage_execution_id: string) {
  return useSuspenseQuery<StageActivityResponse>({
    queryKey: ["runs", "stage-activity", run_id, stage_execution_id],
    queryFn: () =>
      apiFetch<StageActivityResponse>(
        `/api/pipelines/runs/${run_id}/stages/${stage_execution_id}/activity`,
      ),
  });
}

function _invalidateRun(qc: ReturnType<typeof useQueryClient>, ticket_id: string): void {
  qc.invalidateQueries({ queryKey: ["runs", ticket_id] });
  qc.invalidateQueries({ queryKey: ["runs", "overview", ticket_id] });
  qc.invalidateQueries({ queryKey: ["tickets", ticket_id] });
}

/** Cancel an in-flight (`running`) run — deferred to the next safe checkpoint. */
export function useCancelRun(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (run_id: string) =>
      apiFetch(`/api/pipelines/runs/${run_id}/cancel`, { method: "POST" }),
    onSuccess: () => _invalidateRun(qc, ticket_id),
  });
}

/** Resolve a HITL pause: approve / instruct / send back / kill. */
export function useRespondPause(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { pauseId: string; resolution: PauseResolutionBody }) =>
      apiFetch<{ run_state: string }>(`/api/pipelines/runs/pauses/${vars.pauseId}/respond`, {
        method: "POST",
        body: JSON.stringify(vars.resolution),
      }),
    onSuccess: () => _invalidateRun(qc, ticket_id),
  });
}

/** Instruct & re-run from an earlier stage on a fresh run. */
export function useRerunFromStage(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { fromStage: string; instruction: string }) =>
      apiFetch<{ run_id: string }>("/api/pipelines/runs/rerun", {
        method: "POST",
        body: JSON.stringify({
          ticket_id,
          from_stage: vars.fromStage,
          instruction: vars.instruction,
        }),
      }),
    onSuccess: () => _invalidateRun(qc, ticket_id),
  });
}

/** Re-run a failed/cancelled/killed run from the beginning, on a fresh run. */
export function useRerunRun(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (run_id: string) =>
      apiFetch<{ run_id: string }>(`/api/pipelines/runs/${run_id}/rerun`, {
        method: "POST",
      }),
    onSuccess: () => _invalidateRun(qc, ticket_id),
  });
}

/** Kick off a pipeline run on a ticket — 409 when already in-flight and
 *  `replace_in_flight=false`. Callers should catch the 409 and prompt. */
export function useStartRun(ticket_id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { pipeline_id: string; input_text?: string; replace_in_flight: boolean }) =>
      apiFetch<{ run_id: string }>("/api/pipelines/runs/start", {
        method: "POST",
        body: JSON.stringify({ ticket_id, ...vars }),
      }),
    onSuccess: () => _invalidateRun(qc, ticket_id),
  });
}

// ── Artifacts (Artifacts tab) ────────────────────────────────────────────────

export type ArtifactGroupView = components["schemas"]["ArtifactGroup"];
export type ArtifactDetailView = components["schemas"]["ArtifactDetailResponse"];

/** Every artifact lineage for the ticket, grouped by stage name. */
export function useArtifacts(ticket_id: string) {
  return useSuspenseQuery<ArtifactGroupView[]>({
    queryKey: ["artifacts", ticket_id],
    queryFn: async () => {
      const resp = await apiFetch<{ artifacts: ArtifactGroupView[] }>(
        `/api/artifacts?ticket_id=${ticket_id}`,
      );
      return resp.artifacts;
    },
  });
}

/** One artifact version, body included. `null` while no version is selected. */
export function useArtifactVersion(artifact_id: string | null) {
  return useQuery<ArtifactDetailView | null>({
    queryKey: ["artifacts", "version", artifact_id],
    queryFn: () => apiFetch<ArtifactDetailView>(`/api/artifacts/${artifact_id}`),
    enabled: artifact_id !== null,
  });
}

// ── Pipeline definitions (Org Settings > Pipelines page) ────────────────────

export type PipelineSummaryView = components["schemas"]["PipelineSummary"];
export type PipelineDetailView = components["schemas"]["PipelineDetailResponse"];
export type PipelineDefinitionBody = components["schemas"]["PipelineDefinition"];
export type StageView = PipelineDetailView["stages"][number];
export type PipelineTemplateView = components["schemas"]["TemplateResponse"];
export type ActionInfoView = components["schemas"]["ActionInfo"];

/** Org's pipeline definitions, unflattened (a `call` stage counts as one). */
export function usePipelines() {
  return useSuspenseQuery<PipelineSummaryView[]>({
    queryKey: ["pipelines"],
    queryFn: async () => {
      const resp = await apiFetch<{ pipelines: PipelineSummaryView[] }>("/api/pipelines");
      return resp.pipelines;
    },
  });
}

/** One pipeline's full definition. `enabled` gates the fetch — callers
 *  fetch lazily, e.g. only once the pipeline's Accordion row is expanded. */
export function usePipelineDetail(pipelineId: string, opts: { enabled: boolean }) {
  return useQuery<PipelineDetailView>({
    queryKey: ["pipelines", pipelineId],
    queryFn: () => apiFetch<PipelineDetailView>(`/api/pipelines/${pipelineId}`),
    enabled: opts.enabled,
  });
}

/** The shipped, code-defined pipeline templates ("New from template" picker). */
export function usePipelineTemplates() {
  return useSuspenseQuery<PipelineTemplateView[]>({
    queryKey: ["pipeline-templates"],
    queryFn: async () => {
      const resp = await apiFetch<{ templates: PipelineTemplateView[] }>(
        "/api/pipelines/templates",
      );
      return resp.templates;
    },
  });
}

function _invalidatePipelines(qc: ReturnType<typeof useQueryClient>): void {
  qc.invalidateQueries({ queryKey: ["pipelines"] });
}

export function useCreatePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (definition: PipelineDefinitionBody) =>
      apiFetch<{ id: string }>("/api/pipelines", {
        method: "POST",
        body: JSON.stringify(definition),
      }),
    onSuccess: () => _invalidatePipelines(qc),
  });
}

export function useCreatePipelineFromTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (templateId: string) =>
      apiFetch<{ id: string }>("/api/pipelines/from-template", {
        method: "POST",
        body: JSON.stringify({ template_id: templateId }),
      }),
    onSuccess: () => _invalidatePipelines(qc),
  });
}

export function useUpdatePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; definition: PipelineDefinitionBody }) =>
      apiFetch<PipelineDetailView>(`/api/pipelines/${vars.id}`, {
        method: "PUT",
        body: JSON.stringify(vars.definition),
      }),
    onSuccess: (_data, vars) => {
      _invalidatePipelines(qc);
      qc.invalidateQueries({ queryKey: ["pipelines", vars.id] });
    },
  });
}

export function useDeletePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/pipelines/${id}`, { method: "DELETE" }),
    onSuccess: () => _invalidatePipelines(qc),
  });
}

/** `GET /api/actions` — the Pipelines page's "Add an action" stage picker. */
export function useActions() {
  return useSuspenseQuery<ActionInfoView[]>({
    queryKey: ["actions"],
    queryFn: async () => {
      const resp = await apiFetch<{ actions: ActionInfoView[] }>("/api/actions");
      return resp.actions;
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

// ── Intake points + repo config (Org Settings > Repos page) ────────────────

export type IntakePointView = components["schemas"]["IntakePoint"];

/** Every registered intake point (webhook + schedule kinds) — the Repos
 *  page's trigger-binding intake picker. */
export function useIntakePoints() {
  return useSuspenseQuery<IntakePointView[]>({
    queryKey: ["intake-points"],
    queryFn: async () => {
      const resp = await apiFetch<{ points: IntakePointView[] }>("/api/intake/points");
      return resp.points;
    },
  });
}

export type RepoAccordionView = components["schemas"]["RepoAccordionEntry"];
export type RepoConfigView = components["schemas"]["RepoConfigResponse"];
export type ProtectedPathSetView = components["schemas"]["ProtectedPathSet"];
export type TriggerBindingView = components["schemas"]["TriggerBinding"];
export type RepoSettingsSpecBody = components["schemas"]["RepoSettingsSpec"];
export type TriggerBindingSpecBody = components["schemas"]["TriggerBindingSpec"];

/** Installed repos joined against `domain/repos` config — the accordion list. */
export function useRepos() {
  return useSuspenseQuery<RepoAccordionView[]>({
    queryKey: ["repos"],
    queryFn: async () => {
      const resp = await apiFetch<{ repos: RepoAccordionView[] }>("/api/repos");
      return resp.repos;
    },
  });
}

/** One repo's full config (bindings + protected-code + auto-approve).
 *  Always 200 — an absent settings row is the model's defaults, so
 *  `enabled` gates the fetch to when the Accordion row is open, not
 *  whether the repo is "configured". */
export function useRepoConfig(repoExternalId: string, opts: { enabled: boolean }) {
  return useQuery<RepoConfigView>({
    queryKey: ["repos", repoExternalId],
    queryFn: () =>
      apiFetch<RepoConfigView>(`/api/repos/config?repo=${encodeURIComponent(repoExternalId)}`),
    enabled: opts.enabled,
  });
}

function _invalidateRepo(qc: ReturnType<typeof useQueryClient>, repoExternalId: string): void {
  qc.invalidateQueries({ queryKey: ["repos"] });
  qc.invalidateQueries({ queryKey: ["repos", repoExternalId] });
}

/** `PUT /api/repos/settings?repo=` — whole-section replace (last-write-wins). */
export function useSaveRepoSettings(repoExternalId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (spec: RepoSettingsSpecBody) =>
      apiFetch(`/api/repos/settings?repo=${encodeURIComponent(repoExternalId)}`, {
        method: "PUT",
        body: JSON.stringify(spec),
      }),
    onSuccess: () => _invalidateRepo(qc, repoExternalId),
  });
}

export function useAddTrigger(repoExternalId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (spec: TriggerBindingSpecBody) =>
      apiFetch<{ id: string }>(`/api/repos/triggers?repo=${encodeURIComponent(repoExternalId)}`, {
        method: "POST",
        body: JSON.stringify(spec),
      }),
    onSuccess: () => _invalidateRepo(qc, repoExternalId),
  });
}

export function useRemoveTrigger(repoExternalId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (bindingId: string) =>
      apiFetch(`/api/repos/triggers/${bindingId}`, { method: "DELETE" }),
    onSuccess: () => _invalidateRepo(qc, repoExternalId),
  });
}
