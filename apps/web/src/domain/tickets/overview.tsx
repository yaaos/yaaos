/**
 * Overview tab — branches on `RunOverview.status`:
 *
 *   - `paused` — attention block: tripped conditions, the pausing stage's
 *     artifact, open residual findings, and four actions (approve / instruct
 *     / send back / kill). All four are disabled with "Waiting on {names}."
 *     when the server-sent `can_respond` is false — no client role math.
 *   - `in_flight` — live card with a Cancel action (destructive confirm).
 *   - `terminal` — outcome card: PR link on success, mono `failure_reason`
 *     on failure/kill/cancel.
 *
 * No run yet (`useRunOverview` resolves `null`) renders an empty state.
 */

import {
  type PauseDetailView,
  type PipelineRunView,
  type RunOutcomeView,
  useArtifactVersion,
  useCancelRun,
  useRespondPause,
  useRunOverview,
  useRuns,
} from "@core/api/public/queries";
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
import { AlertCircle, CheckCircle2, ExternalLink, Loader2, XCircle } from "lucide-react";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";

export function OverviewTab({ ticketId }: { ticketId: string }) {
  const { data: overview, isLoading, isError } = useRunOverview(ticketId);

  if (isLoading) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-40" />
      </div>
    );
  }
  if (isError) {
    return <ErrorBanner message="Couldn't load this ticket's run." />;
  }
  if (!overview) {
    return (
      <EmptyState
        icon={AlertCircle}
        headline="No runs yet."
        body="When a pipeline starts on this ticket, it'll appear here."
      />
    );
  }

  if (overview.status === "paused" && overview.pause) {
    return (
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
  }
  if (overview.status === "in_flight" && overview.run) {
    return <InFlightCard ticketId={ticketId} run={overview.run} />;
  }
  if (overview.status === "terminal" && overview.outcome) {
    return <OutcomeCard outcome={overview.outcome} />;
  }
  return null;
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

function InFlightCard({ ticketId, run }: { ticketId: string; run: PipelineRunView }) {
  const cancel = useCancelRun(ticketId);
  const [showCancel, setShowCancel] = useState(false);
  const currentStage = run.stages[run.stages.length - 1];

  return (
    <div
      className="rounded-md border border-info/40 bg-info/5 p-4"
      data-testid="attention-block"
      data-state="in_flight"
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

function OutcomeCard({ outcome }: { outcome: RunOutcomeView }) {
  const success = outcome.state === "completed";
  const Icon = success ? CheckCircle2 : XCircle;
  return (
    <div
      className="rounded-md border border-border p-4"
      data-testid="attention-block"
      data-state={outcome.state}
    >
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
      {outcome.failure_reason && (
        <pre className="mt-2 text-xs mono whitespace-pre-wrap text-destructive">
          {outcome.failure_reason}
        </pre>
      )}
    </div>
  );
}
