/**
 * Org Settings > Repos. Admins configure per-repo trigger bindings,
 * protected code, and PR auto-approval — one accordion row per repo the
 * GitHub App can see (`GET /api/repos`, joined against `domain/repos`
 * config server-side).
 */

import { useIntakePoints, usePipelines, useRepos } from "@core/api/public/queries";
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
import { Skeleton } from "@shared/components/ui/skeleton";
import { GitFork } from "lucide-react";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { RepoConfigPanel } from "../RepoConfigPanel";

export function RepoSettingsPage() {
  return (
    <OrgSettingsLayout active="repos">
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load repos." onRetry={resetErrorBoundary} />
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
          <RepoSettingsContent />
        </Suspense>
      </ErrorBoundary>
    </OrgSettingsLayout>
  );
}

function RepoSettingsContent() {
  const { data: repos } = useRepos();
  const { data: intakePoints } = useIntakePoints();
  const { data: pipelines } = usePipelines();
  const [openRepo, setOpenRepo] = useState<string | null>(null);

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
      <PageHeader
        title="Repos"
        subtitle="Configure triggers, protected code, and auto-approval per repo."
      />

      {repos.length === 0 ? (
        <EmptyState
          icon={GitFork}
          headline="No repos yet."
          body="Connect GitHub in VCS settings to see repos here."
        />
      ) : (
        <Accordion
          type="single"
          collapsible
          data-testid="repos-list"
          value={openRepo ?? ""}
          onValueChange={(v) => setOpenRepo(v || null)}
        >
          {repos.map((repo) => {
            const configured =
              repo.trigger_count > 0 || repo.has_protected_code || repo.auto_approve_enabled;
            return (
              <AccordionItem
                key={repo.repo_external_id}
                value={repo.repo_external_id}
                data-testid={`repo-row-${repo.repo_external_id}`}
              >
                <AccordionTrigger>
                  <div className="flex flex-1 items-center gap-2 pr-2 text-left">
                    <span className="font-medium">{repo.repo_external_id}</span>
                    {!configured && (
                      <Badge
                        variant="secondary"
                        data-testid={`repo-row-${repo.repo_external_id}-status`}
                      >
                        unconfigured
                      </Badge>
                    )}
                    {repo.trigger_count > 0 && (
                      <Badge variant="outline">
                        {repo.trigger_count} trigger{repo.trigger_count === 1 ? "" : "s"}
                      </Badge>
                    )}
                    {repo.has_protected_code && <Badge variant="outline">Protected</Badge>}
                    {repo.auto_approve_enabled && <Badge variant="outline">Auto-approve</Badge>}
                  </div>
                </AccordionTrigger>
                <AccordionContent>
                  <RepoConfigPanel
                    repoExternalId={repo.repo_external_id}
                    intakePoints={intakePoints}
                    pipelines={pipelines}
                  />
                </AccordionContent>
              </AccordionItem>
            );
          })}
        </Accordion>
      )}
    </div>
  );
}
