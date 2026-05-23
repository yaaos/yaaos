import { PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { PluginPicker, useAvailablePlugins } from "@shared/plugin_picker";
import type { PluginMeta } from "@shared/plugin_picker";
import { useState } from "react";
import { OrgSettingsLayout } from "../OrgSettingsLayout";
import { useClearVcs, useSetVcs, useVcsState } from "./queries";

/**
 * Org Settings > VCS. Two states:
 *
 *  - Empty (`plugin_id === null`): renders the PluginPicker. Picking GitHub
 *    redirects to its install handshake; picking a settings-only plugin
 *    persists immediately.
 *  - Connected: renders the chosen plugin's settings card + a Remove control
 *    behind a confirmation modal. Today only the GitHub plugin ships.
 */
export function VcsSettingsPage() {
  const { data: state, isLoading } = useVcsState();
  const { data: plugins, isLoading: pluginsLoading, error } = useAvailablePlugins("vcs");
  const setVcs = useSetVcs();

  if (isLoading) {
    return (
      <OrgSettingsLayout active="vcs">
        <div className="p-6 text-sm text-muted-foreground">Loading…</div>
      </OrgSettingsLayout>
    );
  }

  const onPick = (p: PluginMeta) => {
    setVcs.mutate(
      { plugin_id: p.id, settings: {} },
      {
        onSuccess: (resp) => {
          if (resp.install_url) {
            window.location.href = resp.install_url;
          }
        },
      },
    );
  };

  return (
    <OrgSettingsLayout active="vcs">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <PageHeader title="VCS" subtitle="Where yaaos pushes review comments. One VCS per org." />
        {state?.plugin_id ? (
          <ConnectedCard plugin_id={state.plugin_id} settings={state.settings} />
        ) : (
          <section className="rounded-lg border border-border bg-card">
            <header className="border-b border-border px-4 py-3">
              <h3 className="text-sm font-semibold">Choose a VCS plugin</h3>
              <p className="text-muted-foreground text-xs mt-1">
                Pick a plugin to start sending pull requests through yaaos.
              </p>
            </header>
            <div className="px-4 py-4">
              <PluginPicker
                plugins={plugins ?? []}
                loading={pluginsLoading}
                error={(error as Error) ?? null}
                onPick={onPick}
                testIdPrefix="vcs-picker"
              />
              {setVcs.isError && (
                <p className="mt-3 text-xs text-destructive" data-testid="vcs-set-err">
                  {(setVcs.error as Error)?.message || "Failed"}
                </p>
              )}
            </div>
          </section>
        )}
      </div>
    </OrgSettingsLayout>
  );
}

function ConnectedCard({
  plugin_id,
  settings,
}: {
  plugin_id: string;
  settings: Record<string, unknown>;
}) {
  const clearVcs = useClearVcs();
  const [confirming, setConfirming] = useState(false);

  return (
    <section className="rounded-lg border border-border bg-card" data-testid="vcs-connected">
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">{plugin_id}</h3>
          <Badge>connected</Badge>
        </div>
      </header>
      <div className="px-4 py-4">
        {plugin_id === "github" ? (
          <GithubConnectedDetails installationId={settings.installation_id as number | undefined} />
        ) : (
          <pre
            className="rounded bg-muted px-3 py-2 text-xs text-muted-foreground overflow-auto"
            data-testid="vcs-settings-json"
          >
            {JSON.stringify(settings, null, 2)}
          </pre>
        )}
        <div className="mt-4 flex items-center gap-2">
          <Button asChild variant="outline" size="sm">
            <a
              href="/api/vcs"
              data-testid="vcs-reconnect"
              onClick={(e) => {
                e.preventDefault();
                window.location.href = "/api/github/install";
              }}
            >
              Reconnect
            </a>
          </Button>
          <Button
            variant="destructive"
            size="sm"
            data-testid="vcs-remove"
            onClick={() => setConfirming(true)}
            disabled={clearVcs.isPending}
          >
            Remove
          </Button>
        </div>
        {confirming && (
          <div
            className="mt-3 rounded-md border border-border bg-muted/50 p-3"
            data-testid="vcs-remove-confirm"
          >
            <p className="mb-2 text-xs">
              Remove this VCS connection? Pull requests will stop syncing into yaaos until a new VCS
              is configured.
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                data-testid="vcs-remove-cancel"
                onClick={() => setConfirming(false)}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                size="sm"
                data-testid="vcs-remove-confirm-btn"
                disabled={clearVcs.isPending}
                onClick={() => {
                  setConfirming(false);
                  clearVcs.mutate();
                }}
              >
                Remove
              </Button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function GithubConnectedDetails({ installationId }: { installationId?: number }) {
  return (
    <div className="text-sm">
      <p className="text-muted-foreground text-xs">
        Installation id: <span className="font-mono">{installationId ?? "—"}</span>
      </p>
      <p className="text-muted-foreground mt-1 text-xs">
        Manage the App + repo allowlist on GitHub via the Reconnect link.
      </p>
    </div>
  );
}
