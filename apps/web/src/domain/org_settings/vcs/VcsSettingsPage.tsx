import { useGithubInstallation, useGithubRepositories } from "@core/api";
import { ErrorBanner, PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { PluginPicker, useAvailablePlugins } from "@shared/plugin_picker";
import type { PluginMeta } from "@shared/plugin_picker";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { OrgSettingsLayout } from "../OrgSettingsLayout";
import { useClearVcs, useSetVcs, useStartGithubInstall, useVcsState } from "./queries";

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
  return (
    <OrgSettingsLayout active="vcs">
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load VCS settings." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div className="p-6 text-sm text-muted-foreground">
              <Skeleton className="h-32" />
            </div>
          }
        >
          <VcsContent />
        </Suspense>
      </ErrorBoundary>
    </OrgSettingsLayout>
  );
}

function VcsContent() {
  const { data: state } = useVcsState();
  const { data: plugins } = useAvailablePlugins("vcs");
  const setVcs = useSetVcs();
  const startGithubInstall = useStartGithubInstall();

  const onPick = (p: PluginMeta) => {
    if (p.id === "github") {
      // GitHub's install handshake is driven by the dedicated POST endpoint
      // (so `X-Org-Slug` + CSRF reach the auth chain). Skip `setVcs` — the
      // install_callback writes the `vcs_state` row itself on first-bind.
      startGithubInstall.mutate(undefined, {
        onSuccess: (resp) => {
          window.location.href = resp.redirect_url;
        },
      });
      return;
    }
    setVcs.mutate({ plugin_id: p.id, settings: {} });
  };

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
      <PageHeader title="VCS" subtitle="Where yaaos pushes review comments. One VCS per org." />
      {state.plugin_id ? (
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
            <PluginPicker plugins={plugins} onPick={onPick} testIdPrefix="vcs-picker" />
            {setVcs.isError && (
              <p className="mt-3 text-xs text-destructive" data-testid="vcs-set-err">
                {(setVcs.error as Error)?.message || "Failed"}
              </p>
            )}
          </div>
        </section>
      )}
    </div>
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
      {plugin_id === "github" ? (
        <GithubCardBody
          installationId={settings.installation_id as number | undefined}
          onRemove={() => setConfirming(true)}
          removePending={clearVcs.isPending}
        />
      ) : (
        <>
          <header className="flex items-center justify-between border-b border-border px-4 py-3">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold">{plugin_id}</h3>
              <Badge>connected</Badge>
            </div>
          </header>
          <div className="px-4 py-4">
            <pre
              className="rounded bg-muted px-3 py-2 text-xs text-muted-foreground overflow-auto"
              data-testid="vcs-settings-json"
            >
              {JSON.stringify(settings, null, 2)}
            </pre>
            <div className="mt-4 flex items-center gap-2">
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
          </div>
        </>
      )}
      {confirming && (
        <div
          className="mx-4 mb-4 rounded-md border border-border bg-muted/50 p-3"
          data-testid="vcs-remove-confirm"
        >
          <p className="mb-2 text-xs">
            Remove this VCS connection? Pull requests will stop syncing. Reinstalling means clicking
            "Install yaaos on GitHub" again.
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
    </section>
  );
}

function GithubCardBody({
  installationId,
  onRemove,
  removePending,
}: {
  installationId?: number;
  onRemove: () => void;
  removePending: boolean;
}) {
  const { data: inst } = useGithubInstallation();
  const isHealthy = inst.installed === true;
  const appUnconfigured = inst.app_configured === false;

  return (
    <>
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">github</h3>
          {isHealthy ? (
            <Badge>connected</Badge>
          ) : (
            <Badge variant="destructive" data-testid="vcs-github-needs-setup">
              needs setup
            </Badge>
          )}
        </div>
      </header>
      <div className="px-4 py-4">
        {isHealthy ? (
          <GithubHealthyDetails inst={inst} />
        ) : (
          <GithubIncompleteDetails
            installationId={installationId}
            appUnconfigured={appUnconfigured}
          />
        )}
        <div className="mt-4 flex items-center gap-2">
          <Button
            variant="destructive"
            size="sm"
            data-testid="vcs-remove"
            onClick={onRemove}
            disabled={removePending}
          >
            Remove
          </Button>
        </div>
      </div>
    </>
  );
}

function GithubHealthyDetails({
  inst,
}: {
  inst: {
    account_login: string | null;
    install_external_id: string | null;
    installations_url: string | null;
  };
}) {
  return (
    <div className="text-sm" data-testid="vcs-github-details">
      <p className="text-muted-foreground text-xs">
        Account: <span className="font-mono">{inst.account_login ?? "—"}</span>
        {" · "}
        Installation id: <span className="font-mono">{inst.install_external_id ?? "—"}</span>
      </p>

      <div className="mt-4">
        <h4 className="text-xs font-semibold mb-2">Enabled repositories</h4>
        <ErrorBoundary
          fallbackRender={() => (
            <p className="text-destructive text-xs" data-testid="vcs-repos-error">
              Couldn't load repositories from GitHub.
            </p>
          )}
        >
          <Suspense
            fallback={<p className="text-muted-foreground text-xs">Loading repositories…</p>}
          >
            <RepoList />
          </Suspense>
        </ErrorBoundary>
      </div>

      {inst.installations_url && (
        <div className="mt-4">
          <Button asChild variant="outline" size="sm">
            <a
              href={inst.installations_url}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="vcs-manage-on-github"
            >
              Manage on GitHub
            </a>
          </Button>
          <p className="text-muted-foreground mt-2 text-xs">
            Add or remove repositories from the allowlist on github.com.
          </p>
        </div>
      )}
    </div>
  );
}

function GithubIncompleteDetails({
  installationId,
  appUnconfigured,
}: {
  installationId?: number;
  appUnconfigured: boolean;
}) {
  const startInstall = useStartGithubInstall();
  return (
    <div className="text-sm" data-testid="vcs-github-incomplete">
      <p className="text-sm">
        {appUnconfigured
          ? "The yaaos GitHub App isn't provisioned on this deployment. Ask a yaaos operator to configure it."
          : "The yaaos GitHub App isn't installed on this org yet."}
      </p>
      {installationId !== undefined && (
        <p className="text-muted-foreground mt-1 text-xs">
          Stored installation id: <span className="font-mono">{installationId}</span>
        </p>
      )}
      {!appUnconfigured && (
        <div className="mt-4">
          <Button
            variant="outline"
            size="sm"
            data-testid="vcs-github-install"
            disabled={startInstall.isPending}
            onClick={() =>
              startInstall.mutate(undefined, {
                onSuccess: (resp) => {
                  window.location.href = resp.redirect_url;
                },
              })
            }
          >
            Install yaaos on GitHub
          </Button>
          {startInstall.isError && (
            <p className="mt-2 text-xs text-destructive" data-testid="vcs-github-install-err">
              {(startInstall.error as Error)?.message || "Couldn't start install"}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function RepoList() {
  const { data } = useGithubRepositories();
  if (data.error) {
    return (
      <p className="text-destructive text-xs" data-testid="vcs-repos-error">
        Couldn't load repositories from GitHub.
      </p>
    );
  }
  const repos = data.repositories ?? [];
  if (repos.length === 0) {
    return (
      <p className="text-muted-foreground text-xs">
        No repositories enabled yet. Use the link below to grant access.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-1" data-testid="vcs-repos-list">
      {repos.map((r) => (
        <li key={r.full_name} className="flex items-center gap-2 text-xs">
          <a
            href={r.html_url}
            target="_blank"
            rel="noopener noreferrer"
            className="font-mono hover:underline"
          >
            {r.full_name}
          </a>
          {r.private && <Badge>private</Badge>}
        </li>
      ))}
    </ul>
  );
}
