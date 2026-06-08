import { useGithubInstallation, useGithubRepositories } from "@core/api/public/queries";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { PageHeader } from "@shared/components/public/layout/page-header";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { OrgSettingsLayout } from "../../OrgSettingsLayout";
import { useClearVcs, useStartGithubInstall, useVcsState } from "../../vcs/queries";

/**
 * Org Settings > VCS. Two states:
 *
 *  - Empty (`plugin_id === null`): renders the Connect GitHub card. Clicking
 *    redirects to the GitHub App install handshake.
 *  - Connected: renders the chosen plugin's settings card + a Remove control
 *    behind a confirmation modal.
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
  const startGithubInstall = useStartGithubInstall();

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
      <PageHeader title="VCS" subtitle="Where yaaos pushes review comments. One VCS per org." />
      {state.plugin_id ? (
        <ConnectedCard plugin_id={state.plugin_id} settings={state.settings} />
      ) : (
        <section className="rounded-lg border border-border bg-card" data-testid="vcs-picker">
          <header className="border-b border-border px-4 py-3">
            <h3 className="text-sm font-semibold">Connect GitHub</h3>
            <p className="text-muted-foreground text-xs mt-1">
              Install the yaaos GitHub App to start sending pull request reviews.
            </p>
          </header>
          <div className="px-4 py-4">
            <Button
              data-testid="vcs-picker-add-github"
              disabled={startGithubInstall.isPending}
              onClick={() =>
                startGithubInstall.mutate(undefined, {
                  onSuccess: (resp) => {
                    window.location.href = resp.redirect_url;
                  },
                })
              }
            >
              Install yaaos on GitHub
            </Button>
            {startGithubInstall.isError && (
              <p className="mt-3 text-xs text-destructive" data-testid="vcs-set-err">
                {(startGithubInstall.error as Error)?.message || "Failed"}
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
