/**
 * Org Settings > Pipelines. Admins compose the org's pipeline definitions —
 * accordion list, per-stage editor, template instantiation.
 */

import { useActions, usePipelines } from "@core/api/public/queries";
import { EmptyState } from "@shared/components/public/layout/empty-state";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { OrgSettingsLayout } from "@shared/components/public/layout/org-settings-layout";
import { PageHeader } from "@shared/components/public/layout/page-header";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@shared/components/ui/accordion";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/public/ago";
import { GitBranch } from "lucide-react";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { ExistingPipelineEditor, NewPipelineCard, type PicklistData } from "../PipelineEditor";
import { TemplateDialog } from "../TemplateDialog";
import { useInstalledCodingAgents } from "../queries";

export function PipelinesSettingsPage() {
  return (
    <OrgSettingsLayout active="pipelines">
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load pipelines." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
              <Skeleton className="h-8 w-48" />
              <Skeleton className="h-24" />
            </div>
          }
        >
          <PipelinesContent />
        </Suspense>
      </ErrorBoundary>
    </OrgSettingsLayout>
  );
}

function PipelinesContent() {
  const { data: pipelines } = usePipelines();
  const { data: agents } = useInstalledCodingAgents();
  const { data: actions } = useActions();

  const picklists: PicklistData = {
    agents,
    actions,
  };

  const [creating, setCreating] = useState(false);
  const [templateDialogOpen, setTemplateDialogOpen] = useState(false);
  const [openPipelineId, setOpenPipelineId] = useState<string | null>(null);

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
      <PageHeader
        title="Pipelines"
        subtitle="Compose the stage sequence a ticket runs through."
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <a href="/yaaos-pipeline-skills.zip" download data-testid="pipelines-download-skills">
                Download skills
              </a>
            </Button>
            <Button
              variant="outline"
              data-testid="pipeline-new-from-template"
              onClick={() => setTemplateDialogOpen(true)}
            >
              New from template
            </Button>
            <Button data-testid="pipeline-new" onClick={() => setCreating(true)}>
              New pipeline
            </Button>
          </>
        }
      />
      <p className="-mt-2 text-xs text-muted-foreground">
        Unzip at the repo root. Adds .claude/skills and .claude/agents.
      </p>

      {creating && (
        <NewPipelineCard
          allPipelines={pipelines}
          picklists={picklists}
          onCancel={() => setCreating(false)}
          onCreated={() => setCreating(false)}
        />
      )}

      {pipelines.length === 0 && !creating ? (
        <EmptyState
          icon={GitBranch}
          headline="No pipelines yet."
          body="Start from a template or compose one from scratch."
        />
      ) : (
        <Accordion
          type="single"
          collapsible
          data-testid="pipelines-list"
          value={openPipelineId ?? ""}
          onValueChange={(v) => setOpenPipelineId(v || null)}
        >
          {pipelines.map((p) => (
            <AccordionItem key={p.id} value={p.id} data-testid={`pipeline-row-${p.id}`}>
              <AccordionTrigger>
                <div className="flex flex-1 items-center gap-2 pr-2 text-left">
                  <span className="font-medium">{p.name}</span>
                  <Badge variant="outline">
                    {p.stage_count} stage{p.stage_count === 1 ? "" : "s"}
                  </Badge>
                  {p.referenced && <Badge variant="secondary">referenced</Badge>}
                  <span className="ml-auto text-xs text-muted-foreground mono">
                    updated {ago(p.updated_at)}
                    {p.updated_by_login ? ` by ${p.updated_by_login}` : ""}
                  </span>
                </div>
              </AccordionTrigger>
              <AccordionContent>
                <ExistingPipelineEditor
                  pipelineId={p.id}
                  allPipelines={pipelines}
                  picklists={picklists}
                />
              </AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>
      )}

      <TemplateDialog
        open={templateDialogOpen}
        onOpenChange={setTemplateDialogOpen}
        onCreated={() => setTemplateDialogOpen(false)}
      />
    </div>
  );
}
