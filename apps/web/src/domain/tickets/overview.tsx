/**
 * Overview tab — branches on `RunOverview.status`:
 *
 *   - no run yet + manual ticket — kickoff card: pipeline picker, optional
 *     prompt, Run button. A 409 from a stale-overview race shows a
 *     kill-and-restart confirm (`kickoff-confirm`).
 *   - no run yet + non-manual ticket — empty state (run starts automatically).
 *   - `paused` — attention block: tripped conditions, the pausing stage's
 *     artifact, open residual findings, and four actions (approve / instruct
 *     / send back / kill). All four are disabled with "Waiting on {names}."
 *     when the server-sent `can_respond` is false — no client role math.
 *   - `in_flight` — live card with a Cancel action (destructive confirm).
 *   - `terminal` — outcome card: PR link on success, mono `failure_reason`
 *     on failure/kill/cancel.
 */

import {
  type PauseDetailView,
  type PipelineRunView,
  type RunOutcomeView,
  useArtifactVersion,
  useAttachments,
  useCancelRun,
  usePipelines,
  useRerunRun,
  useRespondPause,
  useRunOverview,
  useRuns,
  useStartRun,
} from "@core/api/public/queries";
import { useRunActivityTail } from "@core/sse/public/run_activity";
import { ConfirmModal } from "@shared/components/public/layout/confirm-modal";
import { EmptyState } from "@shared/components/public/layout/empty-state";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Markdown } from "@shared/components/public/markdown";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Textarea } from "@shared/components/ui/textarea";
import { ago } from "@shared/utils/public/ago";
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  Paperclip,
  Play,
  XCircle,
} from "lucide-react";
import { type ReactNode, Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";

export function OverviewTab({
  ticketId,
  ticketType,
  onShowRuns,
}: { ticketId: string; ticketType: string; onShowRuns: () => void }) {
  const { data: overview, isLoading, isError } = useRunOverview(ticketId);

  let mainContent: ReactNode;

  if (isLoading) {
    mainContent = (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-40" />
      </div>
    );
  } else if (isError) {
    mainContent = <ErrorBanner message="Couldn't load this ticket's run." />;
  } else if (!overview) {
    if (ticketType === "manual") {
      mainContent = (
        <ErrorBoundary
          fallbackRender={({ resetErrorBoundary }) => (
            <ErrorBanner message="Couldn't load pipelines." onRetry={resetErrorBoundary} />
          )}
        >
          <Suspense fallback={<Skeleton className="h-40" />}>
            <KickoffCard ticketId={ticketId} />
          </Suspense>
        </ErrorBoundary>
      );
    } else {
      mainContent = (
        <EmptyState
          icon={AlertCircle}
          headline="No runs yet."
          body="When a pipeline starts on this ticket, it'll appear here."
        />
      );
    }
  } else if (overview.status === "paused" && overview.pause) {
    mainContent = (
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load the pausing stage." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense fallback={<Skeleton className="h-64" />}>
          <PausedCard ticketId={ticketId} pause={overview.pause} />
        </Suspense>
      </ErrorBoundary>
    );
  } else if (overview.status === "in_flight" && overview.run) {
    mainContent = <InFlightCard ticketId={ticketId} run={overview.run} onShowRuns={onShowRuns} />;
  } else if (overview.status === "terminal" && overview.outcome) {
    mainContent = <OutcomeCard ticketId={ticketId} outcome={overview.outcome} />;
  } else {
    mainContent = null;
  }

  return (
    <div className="flex flex-col gap-4">
      {mainContent}
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load attachments." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense fallback={<Skeleton className="h-16" />}>
          <AttachmentsSection ticketId={ticketId} />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}

function waitingOnLabel(pause: PauseDetailView): string {
  return pause.escalation_logins.length > 0
    ? `Waiting on ${pause.escalation_logins.join(", ")}.`
    : "Waiting on an org admin.";
}

function PausedCard({ ticketId, pause }: { ticketId: string; pause: PauseDetailView }) {
  // Earlier-stage roster for the send-back picker — sourced from the ticket's
  // most recent run (newest-first), which is this pause's own run.
  const { data: runs } = useRuns(ticketId);
  const currentRun: PipelineRunView | undefined = runs[0];
  const earlierStages =
    currentRun?.stages
      .filter((s) => s.kind === "skill" || s.kind === "review")
      .map((s) => s.stage_name)
      .filter((name, i, arr) => name !== pause.stage_name && arr.indexOf(name) === i) ?? [];

  const respond = useRespondPause(ticketId);
  const [instruction, setInstruction] = useState("");
  const [sendBackStage, setSendBackStage] = useState<string>("");
  const [showKill, setShowKill] = useState(false);

  const disabled = !pause.can_respond || respond.isPending;

  return (
    <div
      className="rounded-md border border-warning/40 bg-warning/5 p-4"
      data-testid="attention-block"
      data-state="paused"
    >
      <div className="flex items-center justify-between gap-2 mb-3">
        <h2 className="text-base font-medium">Waiting on a decision — {pause.stage_name}</h2>
        <div className="flex gap-1 flex-wrap justify-end">
          {Object.entries(pause.tripped)
            .filter(([, v]) => Boolean(v))
            .map(([key]) => (
              <Badge key={key} variant="outline" data-testid={`pause-condition-${key}`}>
                {key.replace(/_/g, " ")}
              </Badge>
            ))}
        </div>
      </div>

      {!pause.can_respond && (
        <p className="text-sm text-muted-foreground mb-3" data-testid="pause-waiting-on">
          {waitingOnLabel(pause)}
        </p>
      )}

      {pause.artifact_id && <PauseArtifact artifactId={pause.artifact_id} />}

      {pause.residuals.length > 0 && (
        <div className="mt-3 flex flex-col gap-2" data-testid="pause-residuals">
          {pause.residuals.map((f) => (
            <div key={f.id} className="text-sm border border-border rounded p-2">
              <span className="font-medium capitalize">{f.severity.replace("_", " ")}</span>
              <span className="text-muted-foreground"> · {f.body}</span>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-start gap-2 mt-4">
        <Button
          data-testid="approve-run"
          disabled={disabled}
          onClick={() =>
            respond.mutate({ pauseId: pause.pause_id, resolution: { action: "approve" } })
          }
        >
          Approve
        </Button>

        <div className="flex items-center gap-2">
          <Textarea
            className="w-64 min-h-9 h-9 py-1.5"
            placeholder="Instruction…"
            value={instruction}
            disabled={disabled}
            onChange={(e) => setInstruction(e.target.value)}
          />
          <Button
            variant="outline"
            data-testid="instruct-run"
            disabled={disabled || !instruction.trim()}
            onClick={() =>
              respond.mutate({
                pauseId: pause.pause_id,
                resolution: { action: "instruct", instruction },
              })
            }
          >
            Instruct
          </Button>
        </div>

        {earlierStages.length > 0 && (
          <div className="flex items-center gap-2">
            <Select value={sendBackStage} onValueChange={setSendBackStage} disabled={disabled}>
              <SelectTrigger className="w-40 h-9">
                <SelectValue placeholder="Send back to…" />
              </SelectTrigger>
              <SelectContent>
                {earlierStages.map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              data-testid="send-back-run"
              disabled={disabled || !sendBackStage}
              onClick={() =>
                respond.mutate({
                  pauseId: pause.pause_id,
                  resolution: { action: "send_back", send_back_to_stage: sendBackStage },
                })
              }
            >
              Send back
            </Button>
          </div>
        )}

        <Button
          variant="destructive"
          data-testid="kill-run"
          disabled={disabled}
          onClick={() => setShowKill(true)}
        >
          Kill
        </Button>
      </div>

      <ConfirmModal
        open={showKill}
        onOpenChange={setShowKill}
        title="Kill run?"
        body="This can't be undone."
        confirmLabel="Kill run"
        tone="destructive"
        pending={respond.isPending}
        onConfirm={() => {
          respond.mutate(
            { pauseId: pause.pause_id, resolution: { action: "kill" } },
            { onSettled: () => setShowKill(false) },
          );
        }}
      />
    </div>
  );
}

function PauseArtifact({ artifactId }: { artifactId: string }) {
  const { data, isError } = useArtifactVersion(artifactId);
  return (
    <details className="mt-1 rounded border border-border" data-testid="pause-artifact">
      <summary className="cursor-pointer text-sm px-3 py-2 select-none hover:bg-accent/40">
        View artifact
      </summary>
      {isError && <ErrorBanner className="m-3" message="Couldn't load the artifact." />}
      {!isError && !data && <Skeleton className="h-16 m-3" />}
      {data && (
        <div className="p-3">
          <Markdown>{data.body}</Markdown>
        </div>
      )}
    </details>
  );
}

function InFlightCard({
  ticketId,
  run,
  onShowRuns,
}: { ticketId: string; run: PipelineRunView; onShowRuns: () => void }) {
  const cancel = useCancelRun(ticketId);
  const [showCancel, setShowCancel] = useState(false);
  const currentStage = run.stages[run.stages.length - 1];
  const { lastEvent, connected } = useRunActivityTail(run.id);

  return (
    <div
      className="rounded-md border border-info/40 bg-info/5 p-4"
      data-testid="attention-block"
      data-state="in_flight"
      data-connected={connected ? "true" : "false"}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Loader2 className="w-4 h-4 text-info animate-spin" aria-hidden />
          <span className="font-medium">{run.pipeline_name}</span>
          {currentStage && (
            <span className="text-sm text-muted-foreground">running {currentStage.stage_name}</span>
          )}
        </div>
        <Button variant="outline" onClick={() => setShowCancel(true)} disabled={cancel.isPending}>
          Cancel
        </Button>
      </div>

      {lastEvent && (
        <button
          type="button"
          className="mt-2 w-full text-left text-xs text-muted-foreground hover:text-foreground transition-colors"
          data-testid="overview-live-ticker"
          onClick={onShowRuns}
        >
          <span className="line-clamp-1">{lastEvent.message}</span>
          <span className="ml-1 text-muted-foreground/70">{ago(lastEvent.ts)}</span>
        </button>
      )}

      <ConfirmModal
        open={showCancel}
        onOpenChange={setShowCancel}
        title="Cancel run?"
        body="The run stops at its next safe checkpoint. Work already done stays."
        confirmLabel="Cancel run"
        tone="destructive"
        pending={cancel.isPending}
        onConfirm={() => cancel.mutate(run.id, { onSettled: () => setShowCancel(false) })}
      />
    </div>
  );
}

const RERUNNABLE_RUN_STATES = new Set(["failed", "cancelled", "killed"]);

function OutcomeCard({ ticketId, outcome }: { ticketId: string; outcome: RunOutcomeView }) {
  const success = outcome.state === "completed";
  const Icon = success ? CheckCircle2 : XCircle;
  const rerun = useRerunRun(ticketId);
  const [rerunOpen, setRerunOpen] = useState(false);
  const canRerunRun = RERUNNABLE_RUN_STATES.has(outcome.state);

  return (
    <div
      className="rounded-md border border-border p-4"
      data-testid="attention-block"
      data-state={outcome.state}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Icon
            className={success ? "w-4 h-4 text-success" : "w-4 h-4 text-destructive"}
            aria-hidden
          />
          <span className="font-medium capitalize">{outcome.state}</span>
          {outcome.pr_url && (
            <a
              href={outcome.pr_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
            >
              View PR <ExternalLink className="w-3 h-3" aria-hidden />
            </a>
          )}
        </div>
        {canRerunRun && (
          <Button
            variant="outline"
            size="sm"
            data-testid="rerun-run"
            onClick={() => setRerunOpen(true)}
          >
            Re-run
          </Button>
        )}
      </div>
      {outcome.failure_reason && (
        <pre className="mt-2 text-xs mono whitespace-pre-wrap text-destructive">
          {outcome.failure_reason}
        </pre>
      )}
      <ConfirmModal
        open={rerunOpen}
        onOpenChange={setRerunOpen}
        title="Re-run pipeline?"
        body="Starts a new run from the beginning."
        confirmLabel="Re-run"
        pending={rerun.isPending}
        onConfirm={() => rerun.mutate(outcome.run_id, { onSettled: () => setRerunOpen(false) })}
      />
    </div>
  );
}

/** Read-only attachments list for the ticket Overview tab. Renders nothing
 *  (no header, no empty state) when the ticket has no attachments yet. */
function AttachmentsSection({ ticketId }: { ticketId: string }) {
  const { data: attachments } = useAttachments(ticketId);
  if (attachments.length === 0) return null;

  return (
    <div>
      <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-2 flex items-center gap-1">
        <Paperclip className="w-3 h-3" aria-hidden />
        Attachments
      </h3>
      <ul className="flex flex-col gap-1" data-testid="attachments-list">
        {attachments.map((a) => (
          <li
            key={a.id}
            className="flex items-center gap-2 rounded border border-border px-3 py-2 text-sm"
            data-testid={`attachment-row-${a.id}`}
          >
            <span className="font-medium truncate">{a.filename}</span>
            {a.produced_by_skill && (
              <span className="text-xs text-muted-foreground">· {a.produced_by_skill}</span>
            )}
            <span className="ml-auto text-xs text-muted-foreground shrink-0">
              {ago(a.attached_at)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Kickoff form for manual tickets that have no run yet. */
function KickoffCard({ ticketId }: { ticketId: string }) {
  const { data: pipelines } = usePipelines();
  const startRun = useStartRun(ticketId);
  const [pipelineId, setPipelineId] = useState<string>("");
  const [prompt, setPrompt] = useState<string>("");
  const [showReplace, setShowReplace] = useState(false);
  const [pendingVars, setPendingVars] = useState<{
    pipeline_id: string;
    input_text: string;
  } | null>(null);

  function handleRun() {
    if (!pipelineId) return;
    const vars = { pipeline_id: pipelineId, input_text: prompt.trim() };
    setPendingVars(vars);
    startRun.mutate(
      { ...vars, replace_in_flight: false },
      {
        onError: (err) => {
          // 409 = a run is already in flight; prompt to kill & restart.
          if ((err as Error)?.message?.startsWith("409")) {
            setShowReplace(true);
          }
        },
      },
    );
  }

  function handleReplace() {
    if (!pendingVars) return;
    startRun.mutate(
      { ...pendingVars, replace_in_flight: true },
      { onSettled: () => setShowReplace(false) },
    );
  }

  return (
    <div className="rounded-md border border-border p-4 flex flex-col gap-3">
      <h2 className="text-sm font-medium">Start a run</h2>

      <div>
        <label htmlFor="kickoff-pipeline" className="text-xs text-muted-foreground mb-1 block">
          Pipeline
        </label>
        <Select value={pipelineId} onValueChange={setPipelineId}>
          <SelectTrigger id="kickoff-pipeline" data-testid="kickoff-pipeline" className="w-64 h-9">
            <SelectValue placeholder="Pick a pipeline…" />
          </SelectTrigger>
          <SelectContent>
            {pipelines.map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div>
        <label htmlFor="kickoff-prompt" className="text-xs text-muted-foreground mb-1 block">
          Prompt (optional)
        </label>
        <Textarea
          id="kickoff-prompt"
          data-testid="kickoff-prompt"
          className="min-h-[80px]"
          placeholder="Add instructions for the agent…"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
      </div>

      {startRun.isError && !(startRun.error as Error)?.message?.startsWith("409") && (
        <ErrorBanner message="Couldn't start the run. Try again." />
      )}

      <div>
        <Button
          data-testid="kickoff-run"
          disabled={!pipelineId || startRun.isPending}
          onClick={handleRun}
        >
          <Play className="w-4 h-4 mr-2" aria-hidden />
          {startRun.isPending ? "Starting…" : "Run"}
        </Button>
      </div>

      <ConfirmModal
        open={showReplace}
        onOpenChange={setShowReplace}
        title="Kill existing run?"
        body="A run is already in progress. Kill it and start a new one from the beginning?"
        confirmLabel="Kill & restart"
        tone="destructive"
        pending={startRun.isPending}
        onConfirm={handleReplace}
        testId="kickoff-confirm"
      />
    </div>
  );
}
