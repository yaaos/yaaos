/**
 * Runs tab — one collapsible card per run (newest first, latest run open),
 * a dense table of that run's stage executions, an artifact Sheet per row
 * that produced one, and "Instruct & re-run from here" on a completed
 * skill/review row.
 */

import {
  type PipelineRunView,
  type StageExecutionView,
  useArtifactVersion,
  useRerunFromStage,
  useRuns,
  useStageActivity,
} from "@core/api/public/queries";
import { EmptyState } from "@shared/components/public/layout/empty-state";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Markdown } from "@shared/components/public/markdown";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@shared/components/ui/dialog";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@shared/components/ui/sheet";
import { Skeleton } from "@shared/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@shared/components/ui/table";
import { Textarea } from "@shared/components/ui/textarea";
import { ago } from "@shared/utils/public/ago";
import { cn } from "@shared/utils/public/cn";
import { GitBranch, ListChecks, Play, Wrench } from "lucide-react";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { ActivityEventRow } from "./ActivityEventRow";

export function RunsTab({ ticketId }: { ticketId: string }) {
  const { data: runs } = useRuns(ticketId);

  if (runs.length === 0) {
    return (
      <EmptyState
        icon={ListChecks}
        headline="No runs yet."
        body="When a pipeline starts on this ticket, its runs appear here."
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {runs.map((run, i) => (
        <RunCard key={run.id} ticketId={ticketId} run={run} defaultOpen={i === 0} />
      ))}
    </div>
  );
}

const KIND_ICON: Record<string, typeof Play> = {
  skill: Play,
  review: ListChecks,
  action: Wrench,
  system: GitBranch,
};

function RunCard({
  ticketId,
  run,
  defaultOpen,
}: {
  ticketId: string;
  run: PipelineRunView;
  defaultOpen: boolean;
}) {
  return (
    <details
      className="rounded-md border border-border transition-opacity duration-200"
      data-testid={`run-card-${run.id}`}
      data-state={run.state}
      open={defaultOpen}
    >
      <summary className="cursor-pointer select-none px-3 py-2.5 flex items-center gap-2 text-sm hover:bg-accent/40">
        <span className="font-medium">{run.pipeline_name}</span>
        <Badge variant="outline" className="capitalize">
          {run.state}
        </Badge>
        <span className="text-muted-foreground">by {run.kickoff.actor_login ?? "yaaos"}</span>
        <span className="ml-auto text-xs text-muted-foreground mono">{ago(run.created_at)}</span>
      </summary>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Stage</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Confidence</TableHead>
            <TableHead>Iterations</TableHead>
            <TableHead>Boundary</TableHead>
            <TableHead>Decisions</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {run.stages.map((stage, idx) => (
            <StageRow
              key={`${stage.stage_name}-${idx}`}
              ticketId={ticketId}
              runId={run.id}
              stage={stage}
            />
          ))}
        </TableBody>
      </Table>
    </details>
  );
}

function StageRow({
  ticketId,
  runId,
  stage,
}: {
  ticketId: string;
  runId: string;
  stage: StageExecutionView;
}) {
  const Icon = KIND_ICON[stage.kind] ?? Play;
  const isSystem = stage.kind === "system";
  const latestArtifactId = stage.artifact_ids[stage.artifact_ids.length - 1];
  const [sheetOpen, setSheetOpen] = useState(false);
  const [rerunOpen, setRerunOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);

  const canRerun =
    stage.status === "completed" && (stage.kind === "skill" || stage.kind === "review");
  const hasActivity = stage.kind === "skill" || stage.kind === "review";

  return (
    <>
      <TableRow
        data-testid={`stage-row-${stage.stage_name}`}
        className={cn(isSystem && "text-muted-foreground")}
      >
        <TableCell className="flex items-center gap-1.5">
          <Icon className="w-3.5 h-3.5 shrink-0" aria-hidden />
          {stage.stage_name}
        </TableCell>
        <TableCell className="capitalize">{stage.status}</TableCell>
        <TableCell>
          {stage.confidence && (
            <Badge variant="outline" className="capitalize">
              {stage.confidence}
            </Badge>
          )}
        </TableCell>
        <TableCell>{stage.review_iterations}</TableCell>
        <TableCell className="capitalize">{stage.boundary_outcome ?? "—"}</TableCell>
        <TableCell>
          {stage.decisions.map((d, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: decisions have no stable id
            <div key={i} className="text-xs">
              {d.action} by {d.actor_login ?? "—"} · {ago(d.resolved_at)}
            </div>
          ))}
          {stage.failure_reason && (
            <pre className="mono text-xs text-destructive whitespace-pre-wrap">
              {stage.failure_reason}
            </pre>
          )}
        </TableCell>
        <TableCell className="flex items-center gap-1 justify-end">
          {hasActivity && (
            <Button variant="ghost" size="sm" onClick={() => setActivityOpen((o) => !o)}>
              Activity
            </Button>
          )}
          {latestArtifactId && (
            <Button variant="ghost" size="sm" onClick={() => setSheetOpen(true)}>
              Artifact
            </Button>
          )}
          {canRerun && (
            <Button
              variant="ghost"
              size="sm"
              data-testid="rerun-from-stage"
              onClick={() => setRerunOpen(true)}
            >
              Instruct &amp; re-run
            </Button>
          )}
        </TableCell>
      </TableRow>
      {activityOpen && (
        <TableRow>
          <TableCell colSpan={7} className="p-0">
            <ErrorBoundary
              fallbackRender={({ resetErrorBoundary }) => (
                <ErrorBanner message="Couldn't load activity." onRetry={resetErrorBoundary} />
              )}
            >
              <Suspense fallback={<Skeleton className="h-16 m-2" />}>
                <StageActivityBody runId={runId} stageExecutionId={stage.id} />
              </Suspense>
            </ErrorBoundary>
          </TableCell>
        </TableRow>
      )}
      {latestArtifactId && (
        <ArtifactSheet open={sheetOpen} onOpenChange={setSheetOpen} artifactId={latestArtifactId} />
      )}
      {canRerun && (
        <RerunDialog
          open={rerunOpen}
          onOpenChange={setRerunOpen}
          ticketId={ticketId}
          stageName={stage.stage_name}
        />
      )}
    </>
  );
}

function StageActivityBody({
  runId,
  stageExecutionId,
}: { runId: string; stageExecutionId: string }) {
  const { data } = useStageActivity(runId, stageExecutionId);
  const events = data.activity?.events ?? [];
  if (events.length === 0) {
    return <p className="px-3 py-2 text-xs text-muted-foreground italic">No activity recorded.</p>;
  }
  return (
    <div className="max-h-[300px] overflow-y-auto" data-testid="stage-activity-blob">
      {events.map((ev, i) => (
        <ActivityEventRow
          // biome-ignore lint/suspicious/noArrayIndexKey: events carry a seq but no stable id
          key={i}
          event={{
            ts: ev.ts,
            kind: ev.kind,
            message: ev.message || "(no message)",
            detail: ev.detail ?? null,
          }}
        />
      ))}
    </div>
  );
}

function ArtifactSheet({
  open,
  onOpenChange,
  artifactId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  artifactId: string;
}) {
  const { data } = useArtifactVersion(open ? artifactId : null);
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right">
        <SheetHeader>
          <SheetTitle>{data?.stage_name ?? "Artifact"}</SheetTitle>
        </SheetHeader>
        {data ? <Markdown>{data.body}</Markdown> : <Skeleton className="h-40" />}
      </SheetContent>
    </Sheet>
  );
}

function RerunDialog({
  open,
  onOpenChange,
  ticketId,
  stageName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ticketId: string;
  stageName: string;
}) {
  const [instruction, setInstruction] = useState("");
  const rerun = useRerunFromStage(ticketId);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Instruct &amp; re-run from {stageName}</DialogTitle>
        </DialogHeader>
        <Textarea
          placeholder="What should change this time?"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={rerun.isPending}>
            Cancel
          </Button>
          <Button
            disabled={!instruction.trim() || rerun.isPending}
            onClick={() =>
              rerun.mutate(
                { fromStage: stageName, instruction },
                { onSuccess: () => onOpenChange(false) },
              )
            }
          >
            Re-run
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
