/**
 * WorkspacesPage — live fleet status for the org.
 *
 * Renders workspace agents grouped into four sections:
 * Active / Draining / Unconfigured / Inactive. Sections with zero agents are
 * hidden. Updates live via `agent_changed` SSE → `["agents"]` invalidation.
 *
 * Empty-state rules:
 *  - If the org is not configured AND no agents exist → NotConfiguredBanner.
 *  - If the org is configured AND no agents exist → EmptyState with a CTA to
 *    Settings → Workspaces.
 *  - If any agents exist → section list (NotConfiguredBanner hidden).
 */

import { getCurrentOrgSlug } from "@core/api/public/org-context";
import { useAgents, useConfigStatus } from "@core/api/public/queries";
import { NotConfiguredBanner } from "@core/layout/public/not-configured-banner";
import { EmptyState } from "@shared/components/public/layout/empty-state";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { PageHeader } from "@shared/components/public/layout/page-header";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Link } from "@tanstack/react-router";
import { Server } from "lucide-react";
import { Suspense } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { AgentSections } from "../AgentSections";

export function WorkspacesPage() {
  return (
    <div className="mx-auto max-w-[1200px] px-6 py-6" data-testid="workspaces-page">
      <PageHeader title="Workspaces" subtitle="Live fleet status." />
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load workspaces." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div>
              <Skeleton className="h-8 w-32 mb-4" />
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: skeletons
                  <Skeleton key={i} className="h-28" />
                ))}
              </div>
            </div>
          }
        >
          <WorkspacesContent />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}

function WorkspacesContent() {
  const orgSlug = getCurrentOrgSlug() ?? "";
  const { data: agents } = useAgents(orgSlug);
  const { data: configStatus } = useConfigStatus();

  const isConfigured = configStatus?.configured ?? true;

  if (agents.length === 0) {
    if (!isConfigured) {
      return <NotConfiguredBanner className="mb-4" />;
    }
    return (
      <div data-testid="workspaces-empty">
        <EmptyState
          icon={Server}
          headline="No workspace agents yet."
          body="Workspace agents run code reviews. Set up an agent in Settings → Workspaces."
          action={
            <Link
              to="/org/$slug/settings/workspaces"
              params={(prev) => ({ slug: (prev as { slug?: string }).slug ?? orgSlug })}
              className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
            >
              Configure in Settings
            </Link>
          }
        />
      </div>
    );
  }

  return <AgentSections agents={agents} />;
}
