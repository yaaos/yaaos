import { getCurrentOrgSlug } from "@core/api";
import { ErrorBanner, PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { PluginPicker, useAvailablePlugins } from "@shared/plugin_picker";
import type { PluginMeta } from "@shared/plugin_picker";
import { Link } from "@tanstack/react-router";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { OrgSettingsLayout } from "../OrgSettingsLayout";
import {
  type CodingAgentInstall,
  useCodingAgents,
  useInstallCodingAgent,
  useUninstallCodingAgent,
} from "./queries";

/**
 * Org Settings > Coding Agents (list view). Per-plugin settings live at
 * /orgs/$slug/settings/coding-agents/$pluginId (see plugin_registry.ts).
 */
export function CodingAgentsSettingsPage() {
  return (
    <OrgSettingsLayout active="coding-agents">
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load coding agents." onRetry={resetErrorBoundary} />
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
          <CodingAgentsContent />
        </Suspense>
      </ErrorBoundary>
    </OrgSettingsLayout>
  );
}

function CodingAgentsContent() {
  const { data: installs } = useCodingAgents();
  const { data: plugins } = useAvailablePlugins("coding_agent");
  const slug = getCurrentOrgSlug();
  const install = useInstallCodingAgent();
  const uninstall = useUninstallCodingAgent();
  const [picking, setPicking] = useState(false);

  const installedIds = new Set(installs.map((i) => i.plugin_id));

  const onPick = (p: PluginMeta) => {
    install.mutate({ plugin_id: p.id, settings: {} }, { onSuccess: () => setPicking(false) });
  };

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
      <PageHeader
        title="Coding Agents"
        subtitle="Coding agents that pick up tickets routed to this org."
        actions={
          !picking ? (
            <Button data-testid="ca-add" onClick={() => setPicking(true)}>
              Add coding agent
            </Button>
          ) : null
        }
      />
      {picking && (
        <section className="rounded-lg border border-border bg-card" data-testid="ca-picker-card">
          <header className="flex items-center justify-between border-b border-border px-4 py-3">
            <h3 className="text-sm font-semibold">Add a coding agent</h3>
            <Button
              variant="ghost"
              size="sm"
              data-testid="ca-picker-cancel"
              onClick={() => setPicking(false)}
            >
              Cancel
            </Button>
          </header>
          <div className="px-4 py-4">
            <PluginPicker
              plugins={plugins}
              isInstalled={(p) => installedIds.has(p.id)}
              onPick={onPick}
              testIdPrefix="ca-picker"
            />
            {install.isError && (
              <p className="mt-3 text-xs text-destructive" data-testid="ca-install-err">
                {(install.error as Error)?.message || "Failed"}
              </p>
            )}
          </div>
        </section>
      )}
      {installs.length === 0 ? (
        <p className="text-muted-foreground text-sm" data-testid="ca-empty">
          No coding agents installed yet.
        </p>
      ) : (
        installs.map((row) => (
          <InstallCard
            key={row.plugin_id}
            row={row}
            slug={slug}
            onRemove={(pluginId) => uninstall.mutate(pluginId)}
            removing={uninstall.isPending}
          />
        ))
      )}
    </div>
  );
}

function InstallCard({
  row,
  slug,
  onRemove,
  removing,
}: {
  row: CodingAgentInstall;
  slug: string | null;
  onRemove: (pluginId: string) => void;
  removing: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const settingsHref = slug
    ? `/orgs/${slug}/settings/coding-agents/${row.plugin_id}`
    : `/settings/coding-agents/${row.plugin_id}`;
  return (
    <section
      className="rounded-lg border border-border bg-card px-4 py-4"
      data-testid={`ca-install-${row.plugin_id}`}
    >
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold">{row.plugin_id}</h3>
            <Badge>installed</Badge>
          </div>
          <p className="text-muted-foreground mt-1 text-xs">
            Updated {new Date(row.updated_at).toLocaleString()}
          </p>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link to={settingsHref} data-testid={`ca-settings-${row.plugin_id}`}>
            Settings
          </Link>
        </Button>
        <Button
          variant="destructive"
          size="sm"
          data-testid={`ca-remove-${row.plugin_id}`}
          disabled={removing}
          onClick={() => setConfirming(true)}
        >
          Remove
        </Button>
      </div>
      {confirming && (
        <div
          className="mt-3 rounded-md border border-border bg-muted/50 p-3"
          data-testid={`ca-remove-confirm-${row.plugin_id}`}
        >
          <p className="mb-2 text-xs">
            Remove this coding agent? Its settings will be deleted; reviews already running continue
            to completion.
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              data-testid={`ca-remove-cancel-${row.plugin_id}`}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              data-testid={`ca-remove-confirm-btn-${row.plugin_id}`}
              onClick={() => {
                setConfirming(false);
                onRemove(row.plugin_id);
              }}
            >
              Remove
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
