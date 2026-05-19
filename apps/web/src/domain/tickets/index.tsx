/** Tickets list + detail.
 *
 * List: status-chips filter, repo/kind/author dropdowns, group-by-status toggle,
 *       table rows per the design (verdict dots, source, actor, tokens, updated-ago).
 * Detail: header (status/kind/draft chips, Cancel/Re-review), Review/Audit tabs,
 *         SummaryStrip, AgentCards (queued/running/posted/skipped/failed states with
 *         status banner + live activity feed + finding expansion), Teach-yaaos modal.
 *
 * Live updates flow via `core/sse` (single EventSource at app root invalidates the
 * relevant Query keys; pages refetch). Polling is also enabled at lower frequency
 * as a fallback — see `useTickets` / `useReviewJobsForTicket` refetchIntervals.
 *
 * Activity events are higher-frequency than invalidation can keep up with, so
 * `useLiveActivity(reviewJobId)` reads from an in-memory ring buffer fed
 * directly by the SSE subscriber. Merged with `job.activity_log` (persisted
 * history) so tab-close-then-reopen still shows the full feed.
 */
import {
  type Finding,
  type FindingRow,
  type ReviewJob,
  type ReviewJobActivityEvent,
  type ReviewTimelineRow,
  type ThreadMessage,
  type Ticket,
  useCancelReviewerJobs,
  useConversationsForTicket,
  useCreateLesson,
  useFindingsForTicket,
  useFullRereviewMutation,
  useGithubRepositories,
  useRereviewMutation,
  useReviewJobsForTicket,
  useReviewsForTicket,
  useThreadForFinding,
  useTicket,
  useTicketAudit,
  useTickets,
} from "@core/api";
import { useLiveActivity } from "@core/sse";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  Dialog,
  DialogBody,
  DialogFooter,
  DialogHeader,
} from "@shared/components";
import { ago, formatTime } from "@shared/utils/ago";
import { cn } from "@shared/utils/cn";
import { Link, useParams } from "@tanstack/react-router";
import { Github, RefreshCw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

// ─── Tickets list ────────────────────────────────────────────────────────────

type StatusFilter = "all" | "review" | "done";

export function TicketsPage() {
  const { data: tickets, isLoading, isError, error } = useTickets();
  const { data: githubRepos } = useGithubRepositories();
  const [status, setStatus] = useState<StatusFilter>("all");
  const [repo, setRepo] = useState<string>("");
  const [kind, setKind] = useState<string>("");
  const [author, setAuthor] = useState<string>("");
  const [groupBy, setGroupBy] = useState<"none" | "status">("none");

  // Build dropdown options from current data + live install.
  const repoOptions = useMemo(() => {
    const fromInstall = new Set((githubRepos?.repositories ?? []).map((r) => r.full_name));
    const fromTickets = new Set((tickets ?? []).map((t) => t.repo_external_id).filter(Boolean));
    return Array.from(new Set([...fromInstall, ...fromTickets])).sort();
  }, [tickets, githubRepos]);
  const authorOptions = useMemo(() => {
    const set = new Set<string>();
    for (const t of tickets ?? []) {
      if (t.author_login) set.add(t.author_login);
    }
    return Array.from(set).sort();
  }, [tickets]);
  // M01-DELTAS: ticket "kind" is hardcoded "feature" — single option, but the
  // dropdown is still rendered so the UI matches the design and future kinds
  // slot in without a layout shuffle.
  const kindOptions = ["feature"];

  const filtered = useMemo(() => {
    return (tickets ?? []).filter((t) => {
      if (status === "review" && t.status !== "in_review") return false;
      if (status === "done" && t.status !== "complete") return false;
      if (repo && t.repo_external_id !== repo) return false;
      if (author && t.author_login !== author) return false;
      // kind is hardcoded "feature"; filter is a no-op for now but the
      // string-equality check is harmless if a future ticket sets kind elsewhere.
      if (kind && kind !== "feature") return false;
      return true;
    });
  }, [tickets, status, repo, author, kind]);

  const counts = useMemo(
    () => ({
      all: (tickets ?? []).length,
      review: (tickets ?? []).filter((t) => t.status === "in_review").length,
      done: (tickets ?? []).filter((t) => t.status === "complete").length,
    }),
    [tickets],
  );

  return (
    <div className="mx-auto max-w-[1280px]">
      <div className="mb-4">
        <h1 className="text-[20px] font-semibold tracking-tight">Tickets</h1>
        <p className="text-text-3 text-[12.5px] mt-1">One per PR. Updates live as agents review.</p>
      </div>

      <FilterBar
        status={status}
        setStatus={setStatus}
        repo={repo}
        setRepo={setRepo}
        kind={kind}
        setKind={setKind}
        author={author}
        setAuthor={setAuthor}
        groupBy={groupBy}
        setGroupBy={setGroupBy}
        repoOptions={repoOptions}
        kindOptions={kindOptions}
        authorOptions={authorOptions}
        counts={counts}
      />

      {isLoading && <div className="text-text-3 text-[12.5px]">Loading…</div>}
      {isError && (
        <div className="text-danger text-[12.5px]" data-testid="tickets-error">
          {(error as Error).message}
        </div>
      )}
      {filtered.length === 0 && !isLoading && (
        <div className="text-text-3 text-[12.5px]" data-testid="tickets-empty">
          {(tickets ?? []).length === 0
            ? "No tickets yet. Open a PR on a repo where yaaos's GitHub App is installed."
            : "No tickets match the current filters."}
        </div>
      )}

      {groupBy === "status" ? (
        <GroupedList tickets={filtered} />
      ) : (
        <TicketTable tickets={filtered} grouped={false} />
      )}
    </div>
  );
}

function FilterBar({
  status,
  setStatus,
  repo,
  setRepo,
  kind,
  setKind,
  author,
  setAuthor,
  groupBy,
  setGroupBy,
  repoOptions,
  kindOptions,
  authorOptions,
  counts,
}: {
  status: StatusFilter;
  setStatus: (s: StatusFilter) => void;
  repo: string;
  setRepo: (s: string) => void;
  kind: string;
  setKind: (s: string) => void;
  author: string;
  setAuthor: (s: string) => void;
  groupBy: "none" | "status";
  setGroupBy: (s: "none" | "status") => void;
  repoOptions: string[];
  kindOptions: string[];
  authorOptions: string[];
  counts: { all: number; review: number; done: number };
}) {
  return (
    <div className="flex items-center justify-between gap-3 flex-wrap mb-3.5">
      <div className="flex items-center gap-1.5 flex-wrap">
        <StatusChip active={status === "all"} onClick={() => setStatus("all")} variant="soft">
          All <span className="text-text-4 mono ml-1">{counts.all}</span>
        </StatusChip>
        <StatusChip
          active={status === "review"}
          onClick={() => setStatus("review")}
          variant="accent"
        >
          <span className="dot" /> Review{" "}
          <span className="mono ml-1 opacity-70">{counts.review}</span>
        </StatusChip>
        <StatusChip active={status === "done"} onClick={() => setStatus("done")} variant="success">
          <span className="dot" /> Done <span className="mono ml-1 opacity-70">{counts.done}</span>
        </StatusChip>
        <div className="w-2" />
        <FilterSelect label="repo" value={repo} onChange={setRepo} options={repoOptions} />
        <FilterSelect label="kind" value={kind} onChange={setKind} options={kindOptions} />
        <FilterSelect label="author" value={author} onChange={setAuthor} options={authorOptions} />
      </div>
      <div className="flex items-center gap-2">
        <span className="text-text-4 mono text-[10.5px] uppercase tracking-wider">group</span>
        <div className="flex items-center gap-0.5 bg-surface-2 border border-border-soft rounded p-0.5">
          <button
            type="button"
            className={cn(
              "h-[22px] px-2.5 text-[12px] rounded",
              groupBy === "none" ? "bg-surface shadow-sm" : "",
            )}
            onClick={() => setGroupBy("none")}
          >
            None
          </button>
          <button
            type="button"
            className={cn(
              "h-[22px] px-2.5 text-[12px] rounded",
              groupBy === "status" ? "bg-surface shadow-sm" : "",
            )}
            onClick={() => setGroupBy("status")}
          >
            Status
          </button>
        </div>
      </div>
    </div>
  );
}

function StatusChip({
  active,
  onClick,
  children,
  variant,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  variant: "soft" | "accent" | "success";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 rounded-pill px-2 h-[22px] text-[10.5px] font-medium uppercase tracking-wider border",
        active
          ? variant === "accent"
            ? "bg-accent-bg text-accent border-accent-border"
            : variant === "success"
              ? "bg-success/15 text-success border-success/30"
              : "bg-surface-3 text-text border-border-hard"
          : "bg-surface-2 text-text-3 border-border-soft hover:bg-hover",
      )}
    >
      {children}
    </button>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (s: string) => void;
  options: string[];
}) {
  return (
    <label className="inline-flex items-center gap-1 rounded-pill h-[22px] px-2 text-[10.5px] font-medium uppercase tracking-wider bg-surface-2 border border-border-soft text-text-3">
      <span>{label}:</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-transparent border-0 text-[10.5px] font-medium uppercase outline-none cursor-pointer"
      >
        <option value="">all</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

function TicketTable({ tickets, grouped }: { tickets: Ticket[]; grouped: boolean }) {
  if (tickets.length === 0) return null;
  return (
    <div className="border border-border-soft rounded overflow-hidden" data-testid="tickets-list">
      <TicketHead grouped={grouped} />
      {tickets.map((t) => (
        <TicketRow key={t.id} t={t} grouped={grouped} />
      ))}
    </div>
  );
}

function TicketHead({ grouped }: { grouped: boolean }) {
  const cols = grouped ? TIX_COLS_GROUPED : TIX_COLS;
  return (
    <div
      className="grid items-center gap-3 px-3 h-[28px] bg-surface-2 border-b border-border-soft text-text-4 mono text-[10.5px] uppercase tracking-wider"
      style={{ gridTemplateColumns: cols }}
    >
      {!grouped && <span>status</span>}
      <span>title</span>
      <span>kind</span>
      <span>verdicts</span>
      <span />
      <span>author</span>
      <span />
      <span>updated</span>
    </div>
  );
}

const TIX_COLS = "78px 1.6fr 90px 120px 24px 130px 60px 70px";
const TIX_COLS_GROUPED = "1.6fr 90px 120px 24px 130px 60px 70px";

function TicketRow({ t, grouped }: { t: Ticket; grouped: boolean }) {
  const cols = grouped ? TIX_COLS_GROUPED : TIX_COLS;
  return (
    <Link
      to="/orgs/$slug/tickets/$ticketId"
      params={(prev) => ({ slug: prev.slug as string, ticketId: t.id })}
      className="grid items-center gap-3 px-3 h-[44px] border-b border-border-soft last:border-0 hover:bg-hover"
      style={{ gridTemplateColumns: cols }}
      data-testid={`ticket-row-${t.id}`}
    >
      {!grouped && <StatusBadge status={t.status} />}
      <div className="flex flex-col min-w-0 gap-0.5">
        <div className="flex items-center gap-2 min-w-0">
          {t.pr_number != null && (
            <span className="text-text-4 mono text-[11px]">#{t.pr_number}</span>
          )}
          <span className="text-text-3 mono text-[11px] truncate">{t.repo_external_id}</span>
        </div>
        <div className="text-[13px] font-medium truncate">{t.title}</div>
      </div>
      <KindChip />
      <VerdictDots ticketId={t.id} />
      <SourceIcon />
      <div className="flex items-center gap-2 min-w-0">
        {t.author_login && (
          <>
            <Avatar name={t.author_login} />
            <span className="text-[11px] truncate">{t.author_login}</span>
          </>
        )}
      </div>
      <TokensCell ticketId={t.id} />
      <span className="text-text-4 mono text-[11px]">{ago(t.updated_at)}</span>
    </Link>
  );
}

function StatusBadge({ status }: { status: Ticket["status"] }) {
  if (status === "in_review")
    return (
      <Badge variant="accent">
        <span className="dot" />
        Review
      </Badge>
    );
  if (status === "complete")
    return (
      <Badge variant="success">
        <span className="dot" />
        Done
      </Badge>
    );
  if (status === "abandoned") return <Badge variant="soft">Abandoned</Badge>;
  return <Badge variant="default">{status}</Badge>;
}

function KindChip() {
  // Hardcoded "feature" per M01-DELTAS until intake distinguishes kinds.
  return <Badge variant="soft">feature</Badge>;
}

function GroupedList({ tickets }: { tickets: Ticket[] }) {
  const allGroups: Array<{ label: string; status: Ticket["status"]; items: Ticket[] }> = [
    {
      label: "Review",
      status: "in_review",
      items: tickets.filter((t) => t.status === "in_review"),
    },
    { label: "Done", status: "complete", items: tickets.filter((t) => t.status === "complete") },
    {
      label: "Other",
      status: "open",
      items: tickets.filter((t) => t.status !== "in_review" && t.status !== "complete"),
    },
  ];
  const groups = allGroups.filter((g) => g.items.length > 0);
  return (
    <div className="flex flex-col gap-4">
      {groups.map((g) => (
        <div key={g.label}>
          <div className="flex items-center gap-2 mb-1.5">
            <StatusBadge status={g.status} />
            <span className="text-text-3 mono text-[11px]">{g.items.length}</span>
          </div>
          <TicketTable tickets={g.items} grouped={true} />
        </div>
      ))}
    </div>
  );
}

// Per-ticket sub-cells that fetch review-job data lazily (one tiny query per
// row — TanStack Query dedupes, and these update via SSE invalidations).

function VerdictDots({ ticketId }: { ticketId: string }) {
  const { data: jobs } = useReviewJobsForTicket(ticketId);
  // One review job per ticket now; show its single status dot.
  const latest = (jobs ?? [])[0];
  return (
    <div className="flex items-center gap-1.5">
      <VerdictDot job={latest} />
    </div>
  );
}

function VerdictDot({ job }: { job: ReviewJob | undefined }) {
  if (!job) return <span className="w-2 h-2 rounded-sm bg-surface-3 border border-border-soft" />;
  if (job.status === "posted") {
    const findings = (job.findings ?? []) as Finding[];
    const mustFix = findings.some((f) => f.severity === "must-fix");
    return (
      <span
        className={cn(
          "w-2 h-2 rounded-full",
          mustFix ? "bg-danger" : findings.length > 0 ? "bg-text-3" : "bg-success",
        )}
      />
    );
  }
  if (job.status === "running")
    return <span className="w-2 h-2 rounded-full bg-accent animate-pulse" />;
  if (job.status === "queued") return <span className="w-2 h-2 rounded-sm bg-surface-3" />;
  if (job.status === "failed") return <span className="w-2 h-2 rounded-full bg-danger" />;
  return <span className="w-2 h-2 rounded-sm bg-surface-3" />;
}

function TokensCell({ ticketId }: { ticketId: string }) {
  const { data: jobs } = useReviewJobsForTicket(ticketId);
  const total = (jobs ?? []).reduce((s, j) => s + (j.tokens_in ?? 0) + (j.tokens_out ?? 0), 0);
  return (
    <span className="text-text-3 mono text-[11px] tabular-nums">
      {total > 0 ? fmtTokens(total) : "—"}
    </span>
  );
}

function SourceIcon() {
  return <Github size={14} className="text-text-3" aria-label="github" />;
}

function Avatar({ name }: { name: string }) {
  const initial = name?.[0]?.toUpperCase() ?? "?";
  return (
    <div className="w-[18px] h-[18px] rounded-full bg-surface-3 text-text-2 flex items-center justify-center text-[10px] font-semibold flex-none">
      {initial}
    </div>
  );
}

function fmtTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

// ─── Ticket detail ───────────────────────────────────────────────────────────

type TabKey = "review" | "audit";

export function TicketDetailPage() {
  const { ticketId } = useParams({ from: "/orgs/$slug/tickets/$ticketId" });
  const { data: ticket } = useTicket(ticketId);
  const { data: jobs } = useReviewJobsForTicket(ticketId);
  const { data: findings } = useFindingsForTicket(ticketId, true);
  const { data: audit } = useTicketAudit(ticketId);
  const rereview = useRereviewMutation();
  const cancel = useCancelReviewerJobs();
  const [tab, setTab] = useState<TabKey>("review");

  if (!ticket) {
    return <div className="mx-auto max-w-[1100px] text-text-3 text-[12.5px]">Loading…</div>;
  }

  // All review jobs for this ticket (newest first). The latest one drives
  // the AgentCard's live activity stream; SummaryStrip aggregates across
  // every run. Durable findings (`useFindingsForTicket`) are the source of
  // truth for the count — JSONB `job.findings` caches are per-review and
  // would undercount on multi-review tickets.
  const allJobs = jobs ?? [];
  const latest = allJobs[0];
  const findingsCount = (findings ?? []).length;

  return (
    <div className="mx-auto max-w-[1100px] flex flex-col gap-4" data-testid="ticket-detail">
      <TicketDetailHeader
        ticket={ticket}
        onRereview={() => rereview.mutate(ticket.id)}
        onCancel={() => cancel.mutate(ticket.id)}
        rereviewing={rereview.isPending}
        cancelling={cancel.isPending}
      />

      <div className="flex items-center gap-0 border-b border-border-soft">
        <TabButton
          id="review"
          active={tab === "review"}
          onClick={() => setTab("review")}
          count={findingsCount}
        >
          Review
        </TabButton>
        <TabButton
          id="audit"
          active={tab === "audit"}
          onClick={() => setTab("audit")}
          count={audit?.length ?? 0}
        >
          Audit log
        </TabButton>
      </div>

      {tab === "review" ? (
        <ReviewTab ticket={ticket} jobs={allJobs} latest={latest} findings={findings ?? []} />
      ) : (
        <AuditTab audit={audit ?? []} />
      )}
    </div>
  );
}

function TicketDetailHeader({
  ticket,
  onRereview,
  onCancel,
  rereviewing,
  cancelling,
}: {
  ticket: Ticket;
  onRereview: () => void;
  onCancel: () => void;
  rereviewing: boolean;
  cancelling: boolean;
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="flex flex-col gap-1.5 flex-1 min-w-0">
        <div className="flex items-center gap-2 text-[11px] text-text-3 mono">
          <span>{ticket.repo_external_id}</span>
        </div>
        <h1 className="text-[20px] font-semibold tracking-tight">
          {ticket.title}
          {ticket.pr_number != null && (
            <>
              {" "}
              <span className="text-text-3 font-normal text-[15px]">
                (
                {ticket.pr_html_url ? (
                  <a
                    href={ticket.pr_html_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mono hover:underline text-text-3 hover:text-text"
                  >
                    #{ticket.pr_number}
                  </a>
                ) : (
                  <span className="mono">#{ticket.pr_number}</span>
                )}
                )
              </span>
            </>
          )}
        </h1>
        <div className="flex items-center gap-1.5 flex-wrap">
          <StatusBadge status={ticket.status} />
          <KindChip />
          {ticket.is_draft && <Badge variant="soft">draft</Badge>}
          {ticket.author_login && (
            <span className="text-text-3 text-[11.5px] ml-1">by @{ticket.author_login}</span>
          )}
        </div>
      </div>
      <div className="flex gap-2 pt-1">
        <Button onClick={onCancel} disabled={cancelling} data-testid="cancel-jobs-button">
          <X size={13} />
          {cancelling ? "Cancelling…" : "Cancel jobs"}
        </Button>
        <Button
          variant="primary"
          onClick={onRereview}
          disabled={rereviewing}
          data-testid="rereview-button"
        >
          <RefreshCw size={13} />
          {rereviewing ? "Scheduling…" : "Re-review"}
        </Button>
      </div>
    </div>
  );
}

function TabButton({
  id,
  active,
  onClick,
  count,
  children,
}: {
  id: string;
  active: boolean;
  onClick: () => void;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      data-testid={`tab-${id}`}
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 px-3 h-[34px] text-[12.5px] font-medium border-b-2 -mb-px",
        active ? "border-accent text-text" : "border-transparent text-text-3 hover:text-text-2",
      )}
    >
      {children}
      <span className="mono text-text-4 text-[11px] tabular-nums">{count}</span>
    </button>
  );
}

function ReviewTab({
  ticket,
  jobs,
  latest,
  findings,
}: {
  ticket: Ticket;
  jobs: ReviewJob[];
  latest: ReviewJob | undefined;
  findings: FindingRow[];
}) {
  // Augment mode (plan §9 + "keep info available"): the new multi-review
  // timeline + thread rendering sits ABOVE the legacy SummaryStrip + AgentCard.
  // SummaryStrip aggregates tokens/latency/lessons across ALL review runs;
  // its Findings counter sources from the durable findings table (count
  // across reviews, not per-review JSONB cache). AgentCard stays bound to
  // the latest run for the live activity-log stream + Teach-yaaos modal.
  return (
    <div className="flex flex-col gap-4">
      <DurableFindingsSummary ticketId={ticket.id} />
      <RereviewButtons ticketId={ticket.id} />
      <AllConversationsSection ticketId={ticket.id} />
      <PerReviewTimeline ticketId={ticket.id} />
      <DurableFindingsSection ticketId={ticket.id} />
      <SummaryStrip jobs={jobs} findings={findings} ticket={ticket} />
      <AgentCard agentName="yaaos" job={latest} repoExternalId={ticket.repo_external_id} />
    </div>
  );
}

function DurableFindingsSummary({ ticketId }: { ticketId: string }) {
  // Plan §9.1 — counters strip: Open / Acknowledged / Resolved + latest
  // review chip.
  const { data: findings } = useFindingsForTicket(ticketId, true);
  const { data: reviews } = useReviewsForTicket(ticketId);
  if (!findings || findings.length === 0) {
    return null;
  }
  const open = findings.filter((f) => f.state === "open").length;
  const acknowledged = findings.filter((f) => f.state === "acknowledged").length;
  const resolved = findings.filter(
    (f) => f.state === "resolved_confirmed" || f.state === "resolved_unverified",
  ).length;
  const stale = findings.filter((f) => f.state === "stale").length;
  const latest = reviews && reviews.length > 0 ? reviews[0] : null;
  const counters: Array<{ label: string; value: number; tone?: "danger" }> = [
    { label: "Open findings", value: open, tone: open > 0 ? "danger" : undefined },
    { label: "Acknowledged", value: acknowledged },
    { label: "Resolved", value: resolved },
    { label: "Stale", value: stale },
  ];
  return (
    <Card className="flex" data-testid="durable-findings-summary">
      {counters.map((c, i) => (
        <div
          key={c.label}
          className={cn("flex-1 px-4 py-3", i > 0 ? "border-l border-border-soft" : "")}
        >
          <div className="text-text-3 text-[10.5px] uppercase tracking-wider font-medium">
            {c.label}
          </div>
          <div
            className={cn(
              "mt-1 mono font-semibold text-[17px]",
              c.tone === "danger" ? "text-danger" : "text-text",
            )}
          >
            {c.value}
          </div>
        </div>
      ))}
      <div className="flex-1 px-4 py-3 border-l border-border-soft">
        <div className="text-text-3 text-[10.5px] uppercase tracking-wider font-medium">
          Latest review
        </div>
        <div className="mt-1 text-[13px] truncate">
          {latest ? (
            <>
              <span className="mono font-semibold">Review {latest.sequence_number}</span>
              <span className="text-text-3"> · {latest.scope_kind}</span>
              {latest.completed_at && (
                <span className="text-text-4"> · {ago(latest.completed_at)}</span>
              )}
            </>
          ) : (
            <span className="text-text-4">none yet</span>
          )}
        </div>
      </div>
    </Card>
  );
}

function RereviewButtons({ ticketId }: { ticketId: string }) {
  // Plan §9.5 — two buttons: incremental Re-review + Full re-review.
  // The legacy single Re-review button lives in the ticket header (TicketDetailHeader);
  // here we surface a Full re-review with a confirm dialog because it's expensive.
  const rereview = useRereviewMutation();
  const full = useFullRereviewMutation();
  return (
    <div className="flex items-center gap-2" data-testid="rereview-buttons">
      <Button
        variant="default"
        onClick={() => rereview.mutate(ticketId)}
        disabled={rereview.isPending}
        data-testid="rereview-incremental-btn"
      >
        {rereview.isPending ? "Scheduling…" : "Re-review (incremental)"}
      </Button>
      <Button
        variant="ghost"
        onClick={() => {
          if (
            window.confirm(
              "Full re-review reruns every yaaos-* subagent on the entire PR diff. This can take 10–15 minutes and consumes more tokens than an incremental re-review. Continue?",
            )
          ) {
            full.mutate(ticketId);
          }
        }}
        disabled={full.isPending}
        data-testid="rereview-full-btn"
      >
        {full.isPending ? "Scheduling…" : "Full re-review"}
      </Button>
    </div>
  );
}

function PerReviewTimeline({ ticketId }: { ticketId: string }) {
  // Plan §9.2 — one collapsible <details> per Review, newest at top, latest
  // expanded by default.
  const { data: reviews } = useReviewsForTicket(ticketId);
  if (!reviews || reviews.length === 0) return null;
  return (
    <Card data-testid="per-review-timeline">
      <div className="px-4 py-3 border-b border-border-soft font-medium">Review timeline</div>
      <ul>
        {reviews.map((r, idx) => (
          <li key={r.id} className="border-b border-border-soft last:border-b-0">
            <details open={idx === 0} className="group">
              <summary className="cursor-pointer list-none px-4 py-2.5 flex items-center gap-3">
                <span className="mono font-semibold text-[13px]">Review {r.sequence_number}</span>
                <span className="text-text-3 text-[12px]">{r.scope_kind}</span>
                <span className="text-text-4 text-[12px]">· {r.trigger_reason}</span>
                <div className="flex-1" />
                <span className="text-text-4 text-[11.5px] mono">
                  {r.completed_at ? ago(r.completed_at) : r.status}
                </span>
              </summary>
              <ReviewSectionBody review={r} ticketId={ticketId} />
            </details>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function ReviewSectionBody({
  review,
  ticketId,
}: {
  review: ReviewTimelineRow;
  ticketId: string;
}) {
  const { data: findings } = useFindingsForTicket(ticketId, true);
  const inThisReview = (findings ?? []).filter((f) => f.first_seen_review_id === review.id);
  return (
    <div className="px-4 py-3 text-[12.5px]">
      <div className="text-text-3 grid grid-cols-2 gap-y-1 gap-x-4 mb-3">
        <div>
          status: <span className="mono text-text">{review.status}</span>
        </div>
        <div>
          model:{" "}
          <span className="mono text-text">
            {review.model ?? "—"}
            {review.effort && <span className="text-text-3"> ({review.effort})</span>}
          </span>
        </div>
        <div>
          tokens:{" "}
          <span className="mono text-text">
            {fmtTokens((review.tokens_in ?? 0) + (review.tokens_out ?? 0))}
          </span>
        </div>
        <div>
          duration:{" "}
          <span className="mono text-text">
            {review.duration_s != null ? fmtDuration(review.duration_s) : "—"}
          </span>
        </div>
        {review.scope_kind === "incremental" && review.scope_prev_sha && (
          <div className="col-span-2">
            scope:{" "}
            <span className="mono text-text">
              {review.scope_prev_sha.slice(0, 7)}..{(review.commit_sha_at_start ?? "").slice(0, 7)}
            </span>
          </div>
        )}
      </div>
      <div className="text-text-3 text-[10.5px] uppercase tracking-wider font-medium mb-1">
        Findings first observed in this review ({inThisReview.length})
      </div>
      {inThisReview.length === 0 ? (
        <div className="text-text-4 text-[12px]">No findings raised in this review.</div>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {inThisReview.map((f) => (
            <FindingTimelineRow
              key={f.id}
              findingId={f.id}
              title={f.title}
              state={f.state}
              severity={f.severity}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function FindingTimelineRow({
  findingId,
  title,
  state,
  severity,
}: {
  findingId: string;
  title: string;
  state: string;
  severity: string;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <li className="border border-border-soft rounded">
      <button
        type="button"
        className="w-full px-3 py-2 flex items-center gap-2 text-left"
        onClick={() => setExpanded((x) => !x)}
        data-testid="finding-timeline-row"
      >
        <SeverityPill severity={severity} />
        <StatePill state={state} />
        <span className="flex-1 truncate">{title}</span>
        <span className="text-text-4 text-[11px]">{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && <FindingThreadPanel findingId={findingId} />}
    </li>
  );
}

function FindingThreadPanel({ findingId }: { findingId: string }) {
  const { data: thread } = useThreadForFinding(findingId);
  if (!thread) return <div className="px-3 py-2 text-text-4 text-[12px]">Loading…</div>;
  return (
    <div className="px-3 py-2 border-t border-border-soft" data-testid="finding-thread-panel">
      {thread.acknowledgment && (
        <div className="mb-2 px-2 py-1.5 rounded bg-text-3/10 text-[12px]" data-testid="ack-banner">
          <span className="font-medium">Acknowledged as {thread.acknowledgment.kind}</span>
          {thread.acknowledgment.rationale && (
            <span className="text-text-3"> — {thread.acknowledgment.rationale}</span>
          )}
          <span className="text-text-4">
            {" "}
            — by @{thread.acknowledgment.made_by_external_id}
            {thread.acknowledgment.created_at &&
              ` on ${formatTime(thread.acknowledgment.created_at)}`}
          </span>
        </div>
      )}
      {thread.messages.length === 0 ? (
        <div className="text-text-4 text-[12px]">No messages yet.</div>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {thread.messages.map((m) => (
            <ThreadMessageRow key={m.id} message={m} />
          ))}
        </ul>
      )}
    </div>
  );
}

// The classifier emits five categorical intents (see
// `domain/reviewer/llm/classifier.py`); the UI collapses the
// `acknowledgment_clear` / `acknowledgment_unclear` pair into the single
// "acknowledgment" badge because the act-vs-confirm split is an internal
// routing concept, not a user-facing distinction.
function displayIntent(intent: string): string {
  if (intent === "acknowledgment_clear" || intent === "acknowledgment_unclear") {
    return "acknowledgment";
  }
  return intent;
}

function ThreadMessageRow({ message }: { message: ThreadMessage }) {
  const isYaaos = message.author_kind === "yaaos";
  return (
    <li
      className={cn(
        "px-2 py-1.5 rounded text-[12px]",
        isYaaos ? "bg-accent-soft/40" : "bg-border-soft/40",
      )}
      data-testid={isYaaos ? "yaaos-message" : "human-message"}
    >
      <div className="flex items-center gap-2 mb-0.5">
        <span className="font-medium mono text-[11px]">
          {isYaaos ? "yaaos" : `@${message.author_external_id}`}
        </span>
        {!isYaaos && message.classified_intent && (
          <span
            className="rounded bg-text-3/20 px-1.5 py-0.5 text-[9.5px] uppercase font-medium"
            data-testid="intent-tag"
          >
            {displayIntent(message.classified_intent)}
          </span>
        )}
        {message.created_at && (
          <span className="text-text-4 text-[10.5px] ml-auto">{ago(message.created_at)}</span>
        )}
      </div>
      <div className="whitespace-pre-wrap text-text">{message.body}</div>
    </li>
  );
}

function AllConversationsSection({ ticketId }: { ticketId: string }) {
  const { data: conversations } = useConversationsForTicket(ticketId);
  if (!conversations || conversations.length === 0) return null;
  return (
    <Card data-testid="all-conversations">
      <details open className="group">
        <summary className="cursor-pointer list-none px-4 py-3 border-b border-border-soft flex items-center justify-between">
          <span className="font-medium">All Conversations</span>
          <span className="text-text-4 text-[12px]">{conversations.length}</span>
        </summary>
        <ul>
          {conversations.map((c) => (
            <ConversationAccordionRow key={c.finding_id} conversation={c} />
          ))}
        </ul>
      </details>
    </Card>
  );
}

// Per conversation: collapsed shows the one-line summary (severity, state,
// title, last preview, reply count). Clicking the row toggles open and lazy-
// loads the full thread via `useThreadForFinding` — the query stays disabled
// until the user opens the accordion, so we don't fan out one request per
// conversation on page load. Messages reuse `ThreadMessageRow` for consistent
// yaaos-vs-developer styling.
function ConversationAccordionRow({
  conversation,
}: {
  conversation: ReturnType<typeof useConversationsForTicket>["data"] extends (infer T)[] | undefined
    ? T
    : never;
}) {
  const [open, setOpen] = useState(false);
  const { data: thread } = useThreadForFinding(open ? conversation.finding_id : null);
  return (
    <li className="border-b border-border-soft last:border-b-0" data-testid="conversation-row">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full px-4 py-2 flex items-center gap-3 text-left hover:bg-border-soft/30"
      >
        <span
          aria-hidden
          className={cn("text-text-4 text-[11px] transition-transform", open && "rotate-90")}
        >
          ▶
        </span>
        <SeverityPill severity={conversation.severity} />
        <StatePill state={conversation.state} />
        <div className="flex-1 min-w-0">
          <div className="font-medium truncate">{conversation.title}</div>
          <div className="text-text-3 text-[12px] truncate">
            {conversation.last_message_preview}
          </div>
        </div>
        <div className="text-text-4 text-[12px] mono">{conversation.reply_count} replies</div>
      </button>
      {open && (
        <div className="px-4 pb-3" data-testid="conversation-thread">
          {!thread ? (
            <div className="text-text-4 text-[12px] py-2">Loading thread…</div>
          ) : thread.messages.length === 0 ? (
            <div className="text-text-4 text-[12px] py-2">No messages yet.</div>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {thread.messages.map((m) => (
                <ThreadMessageRow key={m.id} message={m} />
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

function DurableFindingsSection({ ticketId }: { ticketId: string }) {
  const [includeTerminal, setIncludeTerminal] = useState(false);
  const { data: findings } = useFindingsForTicket(ticketId, includeTerminal);
  if (!findings || findings.length === 0) return null;
  return (
    <Card data-testid="durable-findings">
      <details className="group">
        <summary className="cursor-pointer list-none px-4 py-3 border-b border-border-soft flex items-center justify-between">
          <span className="font-medium">Durable Findings</span>
          <div className="flex items-center gap-3">
            <label className="text-text-3 text-[12px] flex items-center gap-1">
              <input
                type="checkbox"
                checked={includeTerminal}
                onChange={(e) => setIncludeTerminal(e.target.checked)}
                data-testid="include-terminal-toggle"
              />
              include resolved/stale
            </label>
            <span className="text-text-4 text-[12px]">{findings.length}</span>
          </div>
        </summary>
        <ul>
          {findings.map((f) => (
            <li
              key={f.id}
              className="px-4 py-2 border-b border-border-soft last:border-b-0 flex items-start gap-3"
              data-testid="durable-finding-row"
            >
              <SeverityPill severity={f.severity} />
              <StatePill state={f.state} />
              <div className="flex-1 min-w-0">
                <div className="font-medium">{f.title}</div>
                <div className="text-text-3 text-[12px] mono">
                  {f.file_path}:{f.line_start}
                  {f.line_end !== f.line_start ? `-${f.line_end}` : ""} · {f.rule_id} · confidence{" "}
                  {f.confidence}
                </div>
                {f.body && <div className="text-text-3 text-[12px] mt-1">{f.body}</div>}
              </div>
            </li>
          ))}
        </ul>
      </details>
    </Card>
  );
}

function SeverityPill({ severity }: { severity: string }) {
  const tone =
    severity === "blocker"
      ? "bg-danger text-white"
      : severity === "major"
        ? "bg-text-3 text-white"
        : severity === "minor"
          ? "bg-text-4 text-white"
          : "bg-border-soft text-text-3";
  return (
    <span
      className={cn("inline-block rounded px-1.5 py-0.5 text-[10px] uppercase font-medium", tone)}
    >
      {severity}
    </span>
  );
}

function StatePill({ state }: { state: string }) {
  const tone =
    state === "open"
      ? "bg-danger/15 text-danger"
      : state === "acknowledged"
        ? "bg-text-3/15 text-text-3"
        : state === "resolved_confirmed"
          ? "bg-success/15 text-success"
          : "bg-border-soft text-text-4";
  return (
    <span
      className={cn("inline-block rounded px-1.5 py-0.5 text-[10px] uppercase font-medium", tone)}
    >
      {state.replace("_", " ")}
    </span>
  );
}

function SummaryStrip({
  jobs,
  findings,
  ticket,
}: {
  jobs: ReviewJob[];
  findings: FindingRow[];
  ticket: Ticket;
}) {
  // Findings count + must-fix tally come from the DURABLE findings table —
  // unique findings across every review run (plan §10.10 fingerprint dedup
  // means the same finding seen on two reviews is one row, not two). Per-
  // review JSONB caches would undercount. `tokens` / `latency` / `lessons`
  // remain per-job aggregates: sum of every run's spend + longest run +
  // union of lesson IDs ever applied.
  const totalTokens = jobs.reduce((s, j) => s + (j.tokens_in ?? 0) + (j.tokens_out ?? 0), 0);
  // Plan §10.1 severity tiers — blocker + major correspond to the legacy
  // `must-fix` tier the summary card calls out.
  const mustFix = findings.filter((f) => f.severity === "blocker" || f.severity === "major").length;
  const longest = jobs.reduce((m, j) => Math.max(m, j.duration_s ?? 0), 0);
  const anyRunning = jobs.some((j) => j.status === "running");
  const lessonsApplied = new Set(
    jobs.flatMap((j) => j.lessons_applied ?? []).map((id) => String(id)),
  ).size;

  const cells: Array<{ label: string; value: React.ReactNode; sub: string; tone?: "danger" }> = [
    {
      label: "Findings",
      value: findings.length,
      sub: mustFix > 0 ? `${mustFix} must-fix` : "no must-fixes",
      tone: mustFix > 0 ? "danger" : undefined,
    },
    { label: "Tokens", value: fmtTokens(totalTokens), sub: "in + out" },
    {
      label: "Latency",
      value: anyRunning ? <LiveLatency since={ticket.updated_at} /> : fmtDuration(longest),
      sub: anyRunning ? "in flight" : "longest job",
    },
    { label: "Lessons", value: lessonsApplied, sub: `from ${ticket.repo_external_id}` },
  ];
  return (
    <Card className="flex" data-testid="summary-strip">
      {cells.map((c, i) => (
        <div
          key={c.label}
          className={cn("flex-1 px-4 py-3", i > 0 ? "border-l border-border-soft" : "")}
        >
          <div className="text-text-3 text-[10.5px] uppercase tracking-wider font-medium">
            {c.label}
          </div>
          <div className="mt-1 flex items-baseline gap-1.5">
            <div
              className={cn(
                "mono font-semibold text-[17px]",
                c.tone === "danger" ? "text-danger" : "text-text",
              )}
            >
              {c.value}
            </div>
            <span className="text-text-4 text-[11px]">{c.sub}</span>
          </div>
        </div>
      ))}
    </Card>
  );
}

function LiveLatency({ since }: { since: string }) {
  // Tick once a second so users see the elapsed time grow during a review.
  const [now, setNow] = useState(Date.now());
  useTick(() => setNow(Date.now()), 1000);
  const sec = Math.max(0, Math.floor((now - new Date(since).getTime()) / 1000));
  return (
    <span className="mono text-accent">
      {Math.floor(sec / 60)}m {String(sec % 60).padStart(2, "0")}s
    </span>
  );
}

// Tiny self-contained interval hook to avoid pulling in a util module.
function useTick(fn: () => void, ms: number) {
  // biome-ignore lint/correctness/useExhaustiveDependencies: stable interval
  useMemo(() => {
    const id = setInterval(fn, ms);
    return () => clearInterval(id);
  }, []);
}

function fmtDuration(seconds: number): string {
  if (!seconds) return "—";
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

function AgentCard({
  agentName,
  job,
  repoExternalId,
}: {
  agentName: string;
  job: ReviewJob | undefined;
  repoExternalId: string;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [teachOpen, setTeachOpen] = useState<{ finding: Finding } | null>(null);
  const status = job?.status ?? "no-job";
  const isRunning = status === "running";
  const findings = (job?.findings ?? []) as Finding[];

  return (
    <Card
      className={cn("transition-colors", isRunning ? "border-accent-border shadow-glow" : "")}
      data-testid={`agent-card-${agentName}`}
      data-state={status}
    >
      <CardHeader>
        <AgentAvatar name={agentName} />
        <div className="flex flex-col gap-0.5 flex-1 min-w-0">
          <div className="font-semibold text-[13.5px] capitalize">{agentName}</div>
          <div className="text-text-4 text-[11px] truncate">
            yaaos parent reviewer (dispatches yaaos-* subagents)
            {job?.model || job?.effort ? (
              <span className="ml-2">
                {job.model && <span className="mono">{job.model}</span>}
                {job.effort && (
                  <span className="ml-1">
                    · effort: <span className="mono">{job.effort}</span>
                  </span>
                )}
              </span>
            ) : null}
          </div>
        </div>
        <AgentStatusBadge status={status} />
      </CardHeader>
      <CardContent className="p-0">
        <AgentBody
          status={status}
          job={job}
          findings={findings}
          expanded={expanded}
          setExpanded={setExpanded}
          onTeach={(f) => setTeachOpen({ finding: f })}
        />
      </CardContent>
      {teachOpen && (
        <TeachYaaosModal
          finding={teachOpen.finding}
          repoExternalId={repoExternalId}
          onClose={() => setTeachOpen(null)}
        />
      )}
    </Card>
  );
}

function AgentAvatar({ name }: { name: string }) {
  return (
    <div className="w-[22px] h-[22px] rounded bg-accent-bg text-accent text-[11px] font-semibold grid place-items-center flex-none uppercase">
      {name[0]}
    </div>
  );
}

function AgentStatusBadge({ status }: { status: string }) {
  if (status === "posted") return <Badge variant="success">posted</Badge>;
  if (status === "running")
    return (
      <Badge variant="accent">
        <span className="dot animate-pulse" />
        running
      </Badge>
    );
  if (status === "queued") return <Badge variant="soft">queued</Badge>;
  if (status === "skipped") return <Badge variant="soft">skipped</Badge>;
  if (status === "failed") return <Badge variant="danger">failed</Badge>;
  if (status === "cancelled") return <Badge variant="soft">cancelled</Badge>;
  return <Badge variant="default">no run yet</Badge>;
}

function AgentBody({
  status,
  job,
  findings,
  expanded,
  setExpanded,
  onTeach,
}: {
  status: string;
  job: ReviewJob | undefined;
  findings: Finding[];
  expanded: number | null;
  setExpanded: (n: number | null) => void;
  onTeach: (f: Finding) => void;
}) {
  if (status === "no-job") {
    return (
      <div className="px-4 py-4 text-text-3 text-[12.5px]">
        No review run yet. Click <b>Re-review</b> to schedule one.
      </div>
    );
  }
  return (
    <div className="flex flex-col">
      <StatusBanner status={status} job={job} findingsCount={findings.length} />
      {job && <ActivityFeed job={job} />}
      {status === "posted" && findings.length > 0 && (
        <ul data-testid="findings-list" className="border-t border-border-soft">
          {findings.map((f, i) => {
            const key = `${f.file ?? ""}:${f.line_start ?? 0}:${f.title}`;
            return (
              <li key={key} className="border-t border-border-soft first:border-t-0">
                <FindingListItem
                  finding={f}
                  open={expanded === i}
                  onToggle={() => setExpanded(expanded === i ? null : i)}
                  onTeach={() => onTeach(f)}
                />
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function StatusBanner({
  status,
  job,
  findingsCount,
}: {
  status: string;
  job: ReviewJob | undefined;
  findingsCount: number;
}) {
  if (status === "queued") {
    return (
      <div className="px-4 py-3 text-text-3 text-[12px] flex items-center gap-2">
        <span className="w-2 h-2 rounded-sm bg-surface-3 border border-border-soft" />
        Waiting for an open slot…
      </div>
    );
  }
  if (status === "running") {
    return (
      <div className="px-4 py-3 flex flex-col gap-2">
        <div className="text-text-2 text-[12px]">
          Running · <span className="mono">{job?.current_step ?? "working"}</span>
        </div>
        <IndeterminateBar />
        {job?.tokens_in != null && (
          <div className="text-text-4 mono text-[11px]">
            tokens{" "}
            <span className="text-text-3">
              {fmtTokens((job.tokens_in ?? 0) + (job.tokens_out ?? 0))}
            </span>
          </div>
        )}
      </div>
    );
  }
  if (status === "skipped") {
    return (
      <div className="px-4 py-3 text-text-3 text-[12px]">
        Skipped: <span className="mono">{job?.skip_reason ?? "(unknown)"}</span>
      </div>
    );
  }
  if (status === "failed") {
    return (
      <div className="px-4 py-3 text-danger text-[12px]">
        Failed: {job?.error_message ?? "(unknown error)"}
      </div>
    );
  }
  if (status === "cancelled") {
    return (
      <div className="px-4 py-3 text-text-3 text-[12px]">
        Cancelled ({job?.skip_reason ?? "user_cancel"})
      </div>
    );
  }
  // posted
  if (findingsCount === 0) {
    return (
      <div className="px-4 py-3 text-success text-[12px] flex items-center gap-2">
        <span className="w-2 h-2 rounded-full bg-success" /> Approved — no findings.
      </div>
    );
  }
  return (
    <div className="px-4 py-3 text-text-2 text-[12px]">
      Posted: <span className="font-medium">{findingsCount}</span> finding
      {findingsCount === 1 ? "" : "s"}
    </div>
  );
}

/** Merge persisted `activity_log` with the live SSE-fed ring buffer, de-duping
 * by (`ts` + `kind` + `message`). Show the newest 10 inline; everything else
 * collapses into `<details>`. Newest at the top.
 */
function ActivityFeed({ job }: { job: ReviewJob }) {
  const live = useLiveActivity(job.id);
  const merged = useMemo(
    () => mergeActivity(job.activity_log ?? [], live),
    [job.activity_log, live],
  );
  if (merged.length === 0) return null;
  const recent = merged.slice(0, 10);
  const rest = merged.slice(10);
  return (
    <div
      className="px-4 py-3 border-t border-border-soft flex flex-col gap-1.5"
      data-testid="activity-feed"
    >
      <div className="text-text-3 text-[10.5px] uppercase tracking-wider font-medium">
        Recent activity
      </div>
      <ul className="flex flex-col gap-1">
        {recent.map((e) => (
          <ActivityRow key={activityKey(e)} event={e} />
        ))}
      </ul>
      {rest.length > 0 && (
        <details className="mt-1">
          <summary className="text-text-3 text-[11.5px] cursor-pointer hover:text-text-2">
            All events ({merged.length})
          </summary>
          <ul className="flex flex-col gap-1 mt-2">
            {rest.map((e) => (
              <ActivityRow key={activityKey(e)} event={e} />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function ActivityRow({ event }: { event: ReviewJobActivityEvent }) {
  return (
    <li className="flex items-baseline gap-2 text-[12px]">
      <span className="text-text-4 mono text-[10.5px] tabular-nums flex-none">
        {formatTime(event.ts)}
      </span>
      <span className="text-text-2 truncate">{event.message}</span>
    </li>
  );
}

function activityKey(e: ReviewJobActivityEvent): string {
  return `${e.ts}:${e.kind}:${e.message}`;
}

/** Merge persisted + live, dedupe by (ts+kind+message), newest first. */
function mergeActivity(
  persisted: ReviewJobActivityEvent[],
  live: ReviewJobActivityEvent[],
): ReviewJobActivityEvent[] {
  const seen = new Set<string>();
  const out: ReviewJobActivityEvent[] = [];
  for (const e of [...persisted, ...live]) {
    const k = activityKey(e);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(e);
  }
  // Newest first.
  out.sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));
  return out;
}

function FindingListItem({
  finding,
  open,
  onToggle,
  onTeach,
}: {
  finding: Finding;
  open: boolean;
  onToggle: () => void;
  onTeach: () => void;
}) {
  const sevColor =
    finding.severity === "must-fix"
      ? "bg-danger"
      : finding.severity === "nit"
        ? "bg-text-3"
        : finding.severity === "suggestion"
          ? "bg-accent"
          : "bg-surface-3";
  return (
    <div>
      <button
        type="button"
        className="w-full text-left px-4 py-2.5 hover:bg-hover flex items-start gap-3"
        onClick={onToggle}
      >
        <span className={cn("w-2 h-2 rounded-full mt-1.5 flex-none", sevColor)} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[12.5px] font-medium truncate">{finding.title}</span>
            <span className="text-text-4 mono text-[10.5px] uppercase tracking-wider">
              {finding.severity}
            </span>
          </div>
          {finding.file && (
            <div className="text-text-4 mono text-[11px] mt-0.5 truncate">
              {finding.file}
              {finding.line_start && (
                <>
                  :{finding.line_start}
                  {finding.line_end && finding.line_end !== finding.line_start
                    ? `-${finding.line_end}`
                    : ""}
                </>
              )}
            </div>
          )}
        </div>
      </button>
      {open && (
        <div className="px-4 pb-3 pt-1 flex flex-col gap-2">
          <p className="text-text-2 text-[12.5px] whitespace-pre-wrap">{finding.body}</p>
          {finding.rationale && (
            <blockquote className="border-l-2 border-border-soft pl-3 text-text-3 text-[12px] italic">
              {finding.rationale}
            </blockquote>
          )}
          {finding.snippet && finding.snippet.length > 0 && (
            <pre className="bg-surface-2 border border-border-soft rounded p-2 text-[11.5px] mono overflow-x-auto">
              {finding.snippet.map((s) => (
                <div
                  key={`${s.line_number}:${s.kind}:${s.text}`}
                  className={cn(
                    s.kind === "add"
                      ? "text-success"
                      : s.kind === "del"
                        ? "text-danger"
                        : "text-text-3",
                  )}
                >
                  <span className="text-text-4 inline-block w-10 text-right pr-2 select-none">
                    {s.line_number}
                  </span>
                  {s.kind === "add" ? "+ " : s.kind === "del" ? "- " : "  "}
                  {s.text}
                </div>
              ))}
            </pre>
          )}
          <LessonChips ids={finding.applied_lesson_ids ?? []} />
          <div>
            <Button data-testid="teach-yaaos" onClick={onTeach}>
              Teach yaaos…
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function LessonChips({ ids }: { ids: string[] }) {
  // Lessons aren't fetched here — we just surface the ids the agent attributed.
  // Future: fetch each lesson by id to render its title. For POC the count alone
  // is enough signal that lessons influenced this finding.
  if (!ids || ids.length === 0) return null;
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-text-4 text-[10.5px] uppercase tracking-wider font-medium">
        applied
      </span>
      <Link
        to="/orgs/$slug/memory"
        params={(prev) => ({ slug: prev.slug as string })}
        className="hover:underline"
      >
        <Badge variant="soft">
          {ids.length} lesson{ids.length === 1 ? "" : "s"}
        </Badge>
      </Link>
    </div>
  );
}

function IndeterminateBar() {
  return (
    <div className="w-full h-1 bg-surface-2 rounded overflow-hidden">
      <div className="h-full bg-accent rounded animate-pulse" style={{ width: "60%" }} />
    </div>
  );
}

function AuditTab({
  audit,
}: {
  audit: Array<{
    id: string;
    kind: string;
    created_at: string;
    payload: unknown;
    actor: { kind: string; login: string | null; agent_id: string | null };
  }>;
}) {
  const [open, setOpen] = useState<string | null>(null);
  if (audit.length === 0) {
    return <div className="text-text-3 text-[12.5px]">No audit entries yet.</div>;
  }
  return (
    <ul className="border border-border-soft rounded overflow-hidden" data-testid="audit-log">
      {audit.map((e) => (
        <li key={e.id} className="border-b border-border-soft last:border-0">
          <button
            type="button"
            onClick={() => setOpen(open === e.id ? null : e.id)}
            className="w-full text-left px-3 py-2 hover:bg-hover flex items-center gap-3 text-[11.5px] mono"
          >
            <span className="text-text-4">{formatTime(e.created_at)}</span>
            <span className="text-text-2 flex-1">{e.kind}</span>
            <span className="text-text-4">
              [{e.actor.kind}
              {e.actor.login ? `:${e.actor.login}` : ""}]
            </span>
          </button>
          {open === e.id && (
            <pre className="bg-surface-2 px-3 py-2 text-[11px] mono overflow-x-auto whitespace-pre-wrap border-t border-border-soft">
              {JSON.stringify(e.payload, null, 2)}
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}

function TeachYaaosModal({
  finding,
  repoExternalId,
  onClose,
}: {
  finding: Finding;
  repoExternalId: string;
  onClose: () => void;
}) {
  const create = useCreateLesson();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState(finding.body);
  const titleRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    titleRef.current?.focus();
  }, []);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !body.trim()) return;
    create.mutate(
      { repo_external_id: repoExternalId, title: title.trim(), body: body.trim() },
      { onSuccess: () => onClose() },
    );
  };
  return (
    <Dialog open={true} onClose={onClose} width="560px">
      <DialogHeader onClose={onClose}>
        <h3 className="font-semibold text-[14px]">Teach yaaos</h3>
        <span className="text-text-4 text-[11px]">
          on <b className="text-text-2 mono">{repoExternalId}</b>
        </span>
      </DialogHeader>
      <DialogBody>
        <form className="flex flex-col gap-3" onSubmit={submit} id="teach-yaaos-form">
          <div className="flex flex-col gap-1">
            <span className="text-text-2 text-[11.5px] font-medium">Title</span>
            <input
              ref={titleRef}
              data-testid="teach-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Don't suggest mocks in tests"
              className="px-2 py-1.5 text-[12.5px] border border-border-soft rounded bg-bg"
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-text-2 text-[11.5px] font-medium">Body</span>
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              maxLength={1000}
              rows={6}
              className="px-2 py-1.5 text-[12px] mono border border-border-soft rounded bg-bg"
            />
            <span className="text-text-4 text-[10.5px]">{body.length} / 1000 chars</span>
          </div>
          {create.isError && (
            <div className="text-danger text-[12px]">{(create.error as Error).message}</div>
          )}
        </form>
      </DialogBody>
      <DialogFooter>
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="primary"
          type="submit"
          form="teach-yaaos-form"
          data-testid="teach-save"
          disabled={create.isPending}
        >
          {create.isPending ? "Saving…" : "Save lesson"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
