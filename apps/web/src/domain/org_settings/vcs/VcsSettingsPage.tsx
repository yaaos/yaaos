import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
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
        <div className="p-6 text-sm text-text-3">Loading…</div>
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
        <h2 className="text-[16px] font-semibold">VCS</h2>
        {state?.plugin_id ? (
          <ConnectedCard plugin_id={state.plugin_id} settings={state.settings} />
        ) : (
          <Card>
            <CardHeader>
              <h3 className="text-[13.5px] font-semibold">Choose a VCS plugin</h3>
            </CardHeader>
            <CardContent>
              <p className="mb-3 text-xs text-text-3">
                One VCS per org. Pick a plugin to start sending pull requests through yaaos.
              </p>
              <PluginPicker
                plugins={plugins ?? []}
                loading={pluginsLoading}
                error={(error as Error) ?? null}
                onPick={onPick}
                testIdPrefix="vcs-picker"
              />
              {setVcs.isError && (
                <p className="mt-3 text-xs text-red-500" data-testid="vcs-set-err">
                  {(setVcs.error as Error)?.message || "Failed"}
                </p>
              )}
            </CardContent>
          </Card>
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
    <Card data-testid="vcs-connected">
      <CardHeader>
        <div className="flex items-center gap-2">
          <h3 className="text-[13.5px] font-semibold">{plugin_id}</h3>
          <Badge variant="success">connected</Badge>
        </div>
      </CardHeader>
      <CardContent>
        {plugin_id === "github" ? (
          <GithubConnectedDetails installationId={settings.installation_id as number | undefined} />
        ) : (
          <pre className="text-xs text-text-3" data-testid="vcs-settings-json">
            {JSON.stringify(settings, null, 2)}
          </pre>
        )}
        <div className="mt-4 flex items-center gap-2">
          <a
            href="/api/vcs"
            data-testid="vcs-reconnect"
            className="rounded border border-border-soft px-3 py-1.5 text-xs hover:bg-hover"
            onClick={(e) => {
              e.preventDefault();
              window.location.href = "/api/github/install";
            }}
          >
            Reconnect
          </a>
          <Button
            data-testid="vcs-remove"
            onClick={() => setConfirming(true)}
            disabled={clearVcs.isPending}
          >
            Remove
          </Button>
        </div>
        {confirming && (
          <div
            className="mt-3 rounded border border-border-soft bg-bg-2 p-3"
            data-testid="vcs-remove-confirm"
          >
            <p className="mb-2 text-xs">
              Remove this VCS connection? Pull requests will stop syncing into yaaos until a new VCS
              is configured.
            </p>
            <div className="flex gap-2">
              <Button data-testid="vcs-remove-cancel" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button
                data-testid="vcs-remove-confirm-btn"
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
      </CardContent>
    </Card>
  );
}

function GithubConnectedDetails({ installationId }: { installationId?: number }) {
  return (
    <div className="text-sm">
      <p className="text-text-3 text-xs">
        Installation id: <span className="font-mono">{installationId ?? "—"}</span>
      </p>
      <p className="text-text-3 mt-1 text-xs">
        Manage the App + repo allowlist on GitHub via the Reconnect link.
      </p>
    </div>
  );
}
