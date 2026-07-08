/**
 * A repo's Accordion content — mounted only while its row is expanded
 * (Radix `AccordionContent` unmounts on close), so `useRepoConfig`'s fetch
 * fires on first expand, mirroring `domain/pipeline_settings`'s
 * `ExistingPipelineEditor` lazy-fetch pattern.
 *
 * Triggers commit independently (`TriggersSection`); protected-code +
 * auto-approval share one local draft and one `PUT /api/repos/settings`
 * Save button (the backend's whole-section replace).
 */

import { apiErrorCode } from "@core/api/public/client";
import {
  type IntakePointView,
  type PipelineSummaryView,
  useRepoConfig,
  useSaveRepoSettings,
} from "@core/api/public/queries";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { useEffect, useState } from "react";
import { AutoApprovalSection } from "./AutoApprovalSection";
import { ProtectedCodeSection } from "./ProtectedCodeSection";
import { TriggersSection } from "./TriggersSection";
import { useOrgMembers } from "./queries";
import { type RepoSettingsDraft, configToDraft, draftToSpec } from "./types";

function saveErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  if (code === "invalid_glob") return "One of these globs doesn't compile — check the syntax.";
  return "Couldn't save these settings.";
}

export function RepoConfigPanel({
  repoExternalId,
  intakePoints,
  pipelines,
}: {
  repoExternalId: string;
  intakePoints: IntakePointView[];
  pipelines: PipelineSummaryView[];
}) {
  const { data: config, isLoading, isError } = useRepoConfig(repoExternalId, { enabled: true });
  const { data: members } = useOrgMembers();
  const save = useSaveRepoSettings(repoExternalId);
  const [draft, setDraft] = useState<RepoSettingsDraft | null>(null);
  const [saved, setSaved] = useState(false);

  // Seed the local draft from the fetched config exactly once — recomputing
  // on every render would clobber in-progress edits on an unrelated query
  // refetch (same rationale as `ExistingPipelineEditor`'s seed-once effect).
  useEffect(() => {
    if (config) setDraft((prev) => prev ?? configToDraft(config));
  }, [config]);

  if (isLoading || !draft || !config) {
    return (
      <div className="flex flex-col gap-2 py-2">
        <Skeleton className="h-8" />
        <Skeleton className="h-24" />
      </div>
    );
  }
  if (isError) {
    return <ErrorBanner message="Couldn't load this repo's config." />;
  }

  return (
    <div className="flex flex-col gap-6 py-2" data-testid={`repo-config-${repoExternalId}`}>
      <TriggersSection
        repoExternalId={repoExternalId}
        bindings={config.bindings}
        intakePoints={intakePoints}
        pipelines={pipelines}
        members={members}
      />
      <ProtectedCodeSection draft={draft} setDraft={setDraft} members={members} />
      <AutoApprovalSection draft={draft} setDraft={setDraft} />

      <div className="flex items-center gap-2">
        <Button
          data-testid="repo-settings-save"
          disabled={save.isPending}
          onClick={() => {
            setSaved(false);
            save.mutate(draftToSpec(draft), { onSuccess: () => setSaved(true) });
          }}
        >
          {save.isPending ? "Saving…" : "Save"}
        </Button>
        {saved && !save.isPending && <span className="text-sm text-muted-foreground">Saved.</span>}
      </div>
      {save.isError && <ErrorBanner message={saveErrorMessage(save.error)} />}
    </div>
  );
}
