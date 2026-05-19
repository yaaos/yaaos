import {
  type OnboardingStatus,
  type Ticket,
  useMetricsSummary,
  useOnboarding,
  useReviewJobsForTicket,
  useTickets,
} from "@core/api";
import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
import { ago } from "@shared/utils/ago";
import { Link } from "@tanstack/react-router";
import { Check } from "lucide-react";

export function DashboardPage() {
  const { data: onboarding, isLoading } = useOnboarding();
  if (isLoading || !onboarding) {
    return (
      <div
        className="mx-auto max-w-[1100px] text-text-3 text-[12.5px]"
        data-testid="dashboard-loading"
      >
        Loading…
      </div>
    );
  }
  const allReady = onboarding.github_app_installed && onboarding.anthropic_key_set;
  return allReady ? <DashboardPopulated /> : <DashboardOnboarding onboarding={onboarding} />;
}

// ─── Onboarding state ────────────────────────────────────────────────────────

type Step = {
  n: number;
  title: string;
  sub: string;
  cta: string;
  to: "/orgs/$slug/settings";
  done: boolean;
};

function DashboardOnboarding({ onboarding }: { onboarding: OnboardingStatus }) {
  const steps: Step[] = [
    {
      n: 1,
      title: "Install the GitHub App",
      sub: "Grant yaaos access to the repos you want reviewed. (Pick repos on GitHub.)",
      cta: "Install",
      to: "/orgs/$slug/settings",
      done: onboarding.github_app_installed,
    },
    {
      n: 2,
      title: "Add your model API key",
      sub: "Anthropic key — yaaos uses Claude Code internally.",
      cta: "Add key",
      to: "/orgs/$slug/settings",
      done: onboarding.anthropic_key_set,
    },
  ];
  const completed = steps.filter((s) => s.done).length;

  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-5" data-testid="dashboard-onboarding">
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <h1 className="text-[20px] font-semibold tracking-tight">Welcome to yaaos</h1>
          <p className="text-text-3 text-[12.5px] mt-1">Two steps to your first review.</p>
        </div>
        <Badge variant="soft" data-testid="onboarding-progress">
          {completed} of {steps.length} complete
        </Badge>
      </div>

      <Card className="overflow-hidden">
        {steps.map((s, i) => (
          <StepRow key={s.n} step={s} isLast={i === steps.length - 1} />
        ))}
      </Card>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Then…</h2>
        </CardHeader>
        <CardContent>
          <p className="text-text-2 text-[12.5px] leading-relaxed">
            Open a PR on an allowlisted repo. yaaos will create a ticket and the three review agents
            (architecture, security, style) will start working. You'll see them progress live on the
            ticket page, and three review comments will appear on the PR within a few minutes.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function StepRow({ step, isLast }: { step: Step; isLast: boolean }) {
  const borderCls = isLast ? "" : "border-b border-border-soft";
  const bgCls = step.done ? "bg-success/10" : "";
  return (
    <div className={`flex items-center gap-3 px-5 py-4 ${borderCls} ${bgCls}`}>
      <div
        className={[
          "w-7 h-7 rounded-full grid place-items-center flex-none text-[12px] font-semibold",
          step.done
            ? "bg-success text-white border border-success"
            : "border border-border-hard text-text-2",
        ].join(" ")}
        aria-label={step.done ? "done" : `step ${step.n}`}
      >
        {step.done ? <Check size={14} /> : step.n}
      </div>
      <div className="flex-1 min-w-0">
        <div
          className={`text-[13px] font-semibold ${
            step.done ? "line-through text-text-3" : "text-text"
          }`}
        >
          {step.title}
        </div>
        <div className="text-text-3 text-[12px] mt-0.5">{step.sub}</div>
      </div>
      {step.done ? (
        <Badge variant="success">
          <Check size={11} />
          Done
        </Badge>
      ) : (
        <Link to={step.to} params={(prev) => ({ slug: prev.slug as string })}>
          <Button variant="primary">{step.cta}</Button>
        </Link>
      )}
    </div>
  );
}

// ─── Populated state ─────────────────────────────────────────────────────────

function DashboardPopulated() {
  const { data: metrics } = useMetricsSummary();
  const { data: tickets } = useTickets();
  const inFlight = (tickets ?? []).filter((t) => t.status === "in_review");
  const openInReview = inFlight.length;

  const totalReviews = metrics?.total_reviews_posted ?? 0;
  const failureRate = metrics?.failure_rate ?? 0;
  const failureCount = metrics?.failure_count ?? 0;

  return (
    <div className="mx-auto max-w-[1200px] flex flex-col gap-5" data-testid="dashboard-populated">
      <div>
        <h1 className="text-[20px] font-semibold tracking-tight">Overview</h1>
        <p className="text-text-3 text-[12.5px] mt-1">all-time</p>
      </div>

      <div className="flex gap-3" data-testid="dashboard-metrics">
        <MetricTile label="Reviews posted" value={totalReviews.toLocaleString()} sub="all-time" />
        <MetricTile label="Open tickets" value={openInReview.toString()} sub="in review" />
        <MetricTile
          label="Failure rate"
          value={`${(failureRate * 100).toFixed(1)}%`}
          sub={`${failureCount} failed`}
        />
      </div>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Live agents · in flight</h2>
          <div className="flex-1" />
          <span className="text-text-3 text-[11px] mono">{openInReview} active</span>
        </CardHeader>
        <CardContent className="p-0">
          {inFlight.length === 0 ? (
            <div
              className="px-4 py-6 text-text-3 text-[12.5px]"
              data-testid="dashboard-no-inflight"
            >
              No tickets in review right now.
            </div>
          ) : (
            <ul data-testid="dashboard-inflight">
              {inFlight.map((t) => (
                <LiveTicketRow key={t.id} ticket={t} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function MetricTile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <Card className="flex-1 px-4 py-3">
      <div className="text-text-3 text-[10.5px] uppercase tracking-wider font-medium">{label}</div>
      <div className="mono font-semibold text-[24px] tracking-tight mt-1">{value}</div>
      <div className="text-text-4 text-[11px] mono mt-1">{sub}</div>
    </Card>
  );
}

function LiveTicketRow({ ticket }: { ticket: Ticket }) {
  const { data: jobs } = useReviewJobsForTicket(ticket.id);
  const latest = (jobs ?? [])[0];
  return (
    <li className="border-t border-border-soft first:border-t-0">
      <Link
        to="/orgs/$slug/tickets/$ticketId"
        params={(prev) => ({ slug: prev.slug as string, ticketId: ticket.id })}
        className="flex flex-col gap-2 px-4 py-3 hover:bg-hover"
      >
        <div className="flex items-center gap-3 min-w-0">
          <span className="text-text-4 mono text-[11px] flex-none">
            {ticket.source_external_id}
          </span>
          <span className="text-text-1 text-[12.5px] font-medium truncate min-w-0 flex-1">
            {ticket.title}
          </span>
          <span className="text-text-4 mono text-[11px] flex-none">{ago(ticket.updated_at)}</span>
        </div>
        <div className="flex items-center gap-6 text-[11.5px]">
          {!latest ? (
            <span className="text-text-4 text-[11px] mono">no jobs yet</span>
          ) : (
            <AgentInline name="yaaos" status={latest.status} />
          )}
        </div>
      </Link>
    </li>
  );
}

function AgentInline({ name, status }: { name: string; status: string }) {
  const variant: "success" | "danger" | "soft" | "default" =
    status === "posted"
      ? "success"
      : status === "failed"
        ? "danger"
        : status === "running"
          ? "default"
          : "soft";
  return (
    <div className="flex items-center gap-2">
      <span className="mono text-text-4 text-[10.5px] uppercase tracking-wider">{name}</span>
      <Badge variant={variant}>{status}</Badge>
    </div>
  );
}
