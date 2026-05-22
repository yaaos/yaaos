import {
  type GithubInstallation,
  type PluginMeta,
  type PluginType,
  type WorkspaceProvider,
  useGithubInstallation,
  useGithubRepositories,
  useOnboarding,
  useOrgSettings,
  usePluginHealth,
  usePluginsList,
  useSetAnthropicKey,
  useSetGithubCredentials,
  useUpdateOrgSettings,
  useWorkspaceConnectionStatus,
} from "@core/api";
import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
import { ago } from "@shared/utils/ago";
import { Activity, Github, Server, Zap } from "lucide-react";
import { useEffect, useState } from "react";

const PLUGIN_TYPE_LABEL: Record<PluginType, string> = {
  vcs: "VCS",
  coding_agent: "Coding agent",
  workspace: "Workspace",
};

export function SettingsPage() {
  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-4">
      <div>
        <h1 className="text-[20px] font-semibold tracking-tight">Settings</h1>
        <p className="text-text-3 text-[12.5px] mt-1">
          M01 has no auth. Single org · single self-hosted install.
        </p>
      </div>

      <GitHubAppCard />
      <ApiKeyCard />
      <WorkspaceSettingsCard />
      <PluginHealthCard />
    </div>
  );
}

// ─── Workspace settings (PHASES item 164) ────────────────────────────────────

function WorkspaceSettingsCard() {
  const { data: settings, isLoading } = useOrgSettings();
  const update = useUpdateOrgSettings();
  // Hydrate the form from the server value on mount + whenever the server
  // updates (e.g. after a save). Local state lets the user edit without
  // round-tripping until they hit Save.
  const [provider, setProvider] = useState<WorkspaceProvider | "">("");
  const [arn, setArn] = useState<string>("");
  useEffect(() => {
    if (!settings) return;
    setProvider(settings.workspace_provider ?? "");
    setArn(settings.registered_iam_arn ?? "");
  }, [settings]);

  // Connection-status polling only matters once the org has committed to
  // remote_agent — otherwise it'll always be `not_configured`.
  const status = useWorkspaceConnectionStatus(settings?.workspace_provider === "remote_agent");

  const dirty =
    !!settings &&
    ((settings.workspace_provider ?? "") !== provider ||
      (settings.registered_iam_arn ?? "") !== arn);

  const remoteRequiresArn = provider === "remote_agent" && !arn.trim();

  const headerBadge = (() => {
    if (!settings) return null;
    if (settings.workspace_provider === "remote_agent") {
      return <Badge variant="soft">remote agent</Badge>;
    }
    if (settings.workspace_provider === "in_memory") {
      return <Badge variant="soft">in-process</Badge>;
    }
    return <Badge variant="danger">not configured</Badge>;
  })();

  return (
    <Card>
      <CardHeader>
        <Server size={15} className="text-text-2" />
        <h2 className="font-semibold text-[13.5px]">Workspace provider</h2>
        <div className="flex-1" />
        <span data-testid="workspace-status">{headerBadge}</span>
      </CardHeader>
      <CardContent>
        {isLoading || !settings ? (
          <div className="text-text-3 text-[12.5px]">Loading…</div>
        ) : (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (!dirty || remoteRequiresArn) return;
              update.mutate({
                workspace_provider: provider === "" ? null : provider,
                registered_iam_arn: arn.trim() === "" ? null : arn.trim(),
              });
            }}
          >
            <p className="text-text-3 text-[12px]">
              <b className="text-text-2">in-process</b> runs reviews inside the backend container —
              convenient for local dev, sized for a single org.
              <br />
              <b className="text-text-2">remote agent</b> dispatches to a deployed WorkspaceAgent
              pod; requires registering the pod's IAM role ARN so identity exchange can verify it.
            </p>
            <label className="flex flex-col gap-1">
              <span className="text-text-2 text-[11.5px] font-medium">Provider</span>
              <select
                data-testid="workspace-provider-select"
                value={provider}
                onChange={(e) => setProvider(e.target.value as WorkspaceProvider | "")}
                className="px-2 py-1.5 text-[12.5px] border border-border-soft rounded bg-bg"
              >
                <option value="">— not configured —</option>
                <option value="in_memory">in-process</option>
                <option value="remote_agent">remote agent</option>
              </select>
            </label>
            {provider === "remote_agent" && (
              <>
                <label className="flex flex-col gap-1">
                  <span className="text-text-2 text-[11.5px] font-medium">Agent IAM role ARN</span>
                  <input
                    data-testid="workspace-arn"
                    type="text"
                    value={arn}
                    onChange={(e) => setArn(e.target.value)}
                    placeholder="arn:aws:iam::123456789012:role/yaaos-agent"
                    className="px-2 py-1.5 text-[12.5px] mono border border-border-soft rounded bg-bg"
                  />
                </label>
                <ConnectionStatusLine
                  state={status.data?.state}
                  podCount={status.data?.pod_count ?? 0}
                  latest={status.data?.latest_heartbeat_at ?? null}
                  saved={settings.workspace_provider === "remote_agent"}
                />
              </>
            )}
            <div className="flex items-center gap-3">
              <Button
                type="submit"
                disabled={!dirty || remoteRequiresArn || update.isPending}
                data-testid="workspace-save"
              >
                Save
              </Button>
              {remoteRequiresArn && (
                <span className="text-danger text-[12px]">
                  remote agent requires a non-empty ARN
                </span>
              )}
              {update.isSuccess && !dirty && (
                <span className="text-success text-[12px]" data-testid="workspace-saved">
                  Saved.
                </span>
              )}
              {update.isError && (
                <span className="text-danger text-[12px]">{(update.error as Error).message}</span>
              )}
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

function ConnectionStatusLine({
  state,
  podCount,
  latest,
  saved,
}: {
  state: "connected" | "lost" | "not_configured" | undefined;
  podCount: number;
  latest: string | null;
  saved: boolean;
}) {
  // Pre-save (the user picked remote_agent but hasn't saved yet) there's no
  // point asking the backend for heartbeat state — show a hint instead.
  if (!saved) {
    return (
      <div className="text-text-3 text-[12px]" data-testid="workspace-connection-hint">
        Save the ARN to start polling for the agent's heartbeat.
      </div>
    );
  }
  if (!state) {
    return <div className="text-text-3 text-[12px]">Loading status…</div>;
  }
  const badge =
    state === "connected" ? (
      <Badge variant="success">connected</Badge>
    ) : state === "lost" ? (
      <Badge variant="danger">no heartbeat</Badge>
    ) : (
      <Badge variant="soft">not configured</Badge>
    );
  return (
    <div className="flex items-center gap-2 text-[12px]" data-testid="workspace-connection-line">
      {badge}
      <span className="text-text-3">
        {podCount} pod{podCount === 1 ? "" : "s"}
        {latest && (
          <>
            {" "}
            · last heartbeat <span className="text-text-4">{ago(latest)}</span>
          </>
        )}
      </span>
    </div>
  );
}

// ─── GitHub App ──────────────────────────────────────────────────────────────

function GitHubAppCard() {
  const { data, isLoading } = useGithubInstallation();
  const headerBadge = (() => {
    if (!data) return null;
    if (data.installed) return <Badge variant="success">installed</Badge>;
    if (data.credentials_configured)
      return <Badge variant="soft">app created · not installed</Badge>;
    return <Badge variant="danger">no app</Badge>;
  })();
  return (
    <Card>
      <CardHeader>
        <Github size={15} className="text-text-2" />
        <h2 className="font-semibold text-[13.5px]">GitHub App</h2>
        <div className="flex-1" />
        <span data-testid="github-status">{headerBadge}</span>
      </CardHeader>
      <CardContent>
        {isLoading || !data ? (
          <div className="text-text-3 text-[12.5px]">Loading…</div>
        ) : !data.credentials_configured ? (
          <NoAppBody />
        ) : !data.installed ? (
          <AppCreatedBody data={data} />
        ) : (
          <InstalledBody data={data} />
        )}
      </CardContent>
    </Card>
  );
}

function GhManifestBanner() {
  const params = new URLSearchParams(window.location.search);
  const err = params.get("gh_manifest_error");
  if (!err) return null;
  return <div className="text-danger text-[12px] mb-2">Couldn't create App: {err}</div>;
}

function InstalledBody({ data }: { data: GithubInstallation }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="text-[12.5px] text-text-2">
        Installed on <b className="text-text mono">@{data.account_login}</b>
        {data.installed_at && (
          <>
            {" "}
            · <span className="text-text-3">{ago(data.installed_at)}</span>
          </>
        )}
      </div>
      <RepositoriesList />
      <div className="flex gap-2">
        {data.installations_url && (
          <a href={data.installations_url} target="_blank" rel="noopener noreferrer">
            <Button>Configure on GitHub</Button>
          </a>
        )}
      </div>
    </div>
  );
}

function RepositoriesList() {
  const { data, isLoading, isError, error } = useGithubRepositories();
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <span className="text-text-2 text-[11.5px] font-medium">Repositories</span>
        {data && (
          <span className="text-text-4 text-[11px] mono">{data.total_count} accessible</span>
        )}
      </div>
      {isLoading ? (
        <div className="text-text-3 text-[12px]">Loading…</div>
      ) : isError ? (
        <div className="text-danger text-[12px]">{(error as Error).message}</div>
      ) : data?.error ? (
        <div className="text-danger text-[12px]">{data.error}</div>
      ) : data && data.repositories.length === 0 ? (
        <div className="text-text-3 text-[12px]">
          No repositories yet. Use <b>Configure on GitHub</b> to pick repos for yaaos to see.
        </div>
      ) : (
        <ul
          className="flex flex-col gap-0.5 max-h-[200px] overflow-y-auto border border-border-soft rounded"
          data-testid="github-repos"
        >
          {data?.repositories.map((r) => (
            <li
              key={r.full_name}
              className="flex items-center gap-2 px-2.5 py-1.5 text-[12px] border-b border-border-soft last:border-0"
            >
              <a
                href={r.html_url}
                target="_blank"
                rel="noopener noreferrer"
                className="mono flex-1 truncate hover:underline"
              >
                {r.full_name}
              </a>
              {r.private && <Badge variant="soft">private</Badge>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AppCreatedBody({ data }: { data: GithubInstallation }) {
  return (
    <div className="flex flex-col gap-2.5">
      <GhManifestBanner />
      <p className="text-text-2 text-[12.5px]">
        App <b className="text-text mono">{data.slug}</b> exists on your GitHub account. Install it
        next — GitHub will let you pick which account and which repos.
      </p>
      <div className="flex gap-2 items-center">
        {data.install_url ? (
          <a href={data.install_url} target="_blank" rel="noopener noreferrer">
            <Button variant="primary">Install on GitHub</Button>
          </a>
        ) : (
          <Button variant="primary" disabled>
            Install on GitHub
          </Button>
        )}
        <a href="/api/github/install">
          <Button variant="ghost" data-testid="connect-github-org">
            Connect to this org
          </Button>
        </a>
      </div>
    </div>
  );
}

function NoAppBody() {
  return (
    <div className="flex flex-col gap-3">
      <GhManifestBanner />
      <p className="text-text-2 text-[12.5px]">
        yaaos needs its own GitHub App. We'll create one on GitHub in one click — yaaos tells GitHub
        the permissions and events it needs.
      </p>
      <ManifestForm />
      <details className="text-[12px] text-text-3">
        <summary className="cursor-pointer hover:text-text-2">
          Already have an App? Enter it manually
        </summary>
        <div className="mt-3">
          <CredentialsForm />
        </div>
      </details>
    </div>
  );
}

function ManifestForm() {
  const [webhookUrl, setWebhookUrl] = useState("");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const origin = window.location.origin;
    const manifest = {
      name: "yaaos",
      url: origin,
      hook_attributes: { url: webhookUrl.trim(), active: true },
      redirect_url: `${origin}/api/github/manifest-callback`,
      setup_url: `${origin}/settings`,
      public: false,
      // `installation` is sent automatically to every App and cannot be
      // listed in default_events — including it makes the manifest invalid.
      default_events: [
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
      ],
      default_permissions: {
        pull_requests: "write",
        contents: "read",
        metadata: "read",
        issues: "write",
      },
    };
    // GitHub's manifest endpoint accepts a POST with a `manifest` form field.
    // We create a transient form and submit it.
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "https://github.com/settings/apps/new";
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "manifest";
    input.value = JSON.stringify(manifest);
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
  };

  return (
    <form className="flex flex-col gap-2" onSubmit={submit}>
      <Field
        label="Webhook URL"
        hint="Where GitHub will deliver events. For laptop dev, paste your smee.io channel URL (smee forwards to localhost:8080)."
      >
        <input
          data-testid="gh-webhook-url"
          type="url"
          required
          value={webhookUrl}
          onChange={(e) => setWebhookUrl(e.target.value)}
          placeholder="https://smee.io/abc123"
          className="px-2 py-1.5 text-[12.5px] mono border border-border-soft rounded bg-bg"
        />
      </Field>
      <div>
        <Button type="submit" variant="primary" data-testid="gh-manifest-create">
          Create GitHub App
        </Button>
      </div>
    </form>
  );
}

function CredentialsForm() {
  const setCreds = useSetGithubCredentials();
  const [appId, setAppId] = useState("");
  const [slug, setSlug] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setCreds.mutate(
      {
        app_id: appId.trim(),
        slug: slug.trim(),
        private_key: privateKey,
        webhook_secret: webhookSecret,
      },
      {
        onSuccess: () => {
          setAppId("");
          setSlug("");
          setPrivateKey("");
          setWebhookSecret("");
        },
      },
    );
  };

  return (
    <div className="flex flex-col gap-3">
      <p className="text-text-2 text-[12.5px]">
        yaaos needs its own GitHub App. Create one at{" "}
        <a
          href="https://github.com/settings/apps/new"
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent hover:underline"
        >
          github.com/settings/apps/new
        </a>{" "}
        with these settings, then paste the values below.
      </p>

      <details className="text-[12px] text-text-3">
        <summary className="cursor-pointer hover:text-text-2">
          What to configure on the GitHub App
        </summary>
        <div className="mt-2 pl-2 border-l-2 border-border-soft flex flex-col gap-1.5">
          <div>
            <b className="text-text-2">Webhook URL:</b> wherever yaaos is reachable +{" "}
            <code className="mono bg-surface-2 px-1 py-0.5 rounded">/api/github/webhook</code> (for
            laptop dev, use your smee.io channel URL).
          </div>
          <div>
            <b className="text-text-2">Webhook secret:</b> any random string. Paste the same value
            below.
          </div>
          <div>
            <b className="text-text-2">Repository permissions:</b> Pull requests (Read & write),
            Contents (Read), Metadata (Read), Issues (Read & write).
          </div>
          <div>
            <b className="text-text-2">Subscribe to events:</b> Pull request, Pull request review,
            Pull request review comment, Issue comment. (Installation events are sent automatically
            — don't tick them.)
          </div>
          <div>
            <b className="text-text-2">Where the App can be installed:</b> Only on this account
            (private — only you can install).
          </div>
          <div>
            <b className="text-text-2">Setup URL (optional):</b> your yaaos URL +{" "}
            <code className="mono bg-surface-2 px-1 py-0.5 rounded">/settings</code> for a nice
            post-install round-trip.
          </div>
        </div>
      </details>

      <form className="flex flex-col gap-2" onSubmit={submit}>
        <Field label="App ID" hint="Numeric, e.g. 1234567">
          <input
            data-testid="gh-app-id"
            type="text"
            value={appId}
            onChange={(e) => setAppId(e.target.value)}
            placeholder="1234567"
            className="px-2 py-1.5 text-[12.5px] mono border border-border-soft rounded bg-bg"
            required
          />
        </Field>
        <Field
          label="App slug"
          hint="The URL-handle, e.g. yaaos-jack — visible at github.com/apps/<slug>"
        >
          <input
            data-testid="gh-slug"
            type="text"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="yaaos-yourname"
            className="px-2 py-1.5 text-[12.5px] mono border border-border-soft rounded bg-bg"
            required
          />
        </Field>
        <Field
          label="Private key (PEM)"
          hint="Generate on the App page and download the .pem file; paste its contents"
        >
          <textarea
            data-testid="gh-pem"
            value={privateKey}
            onChange={(e) => setPrivateKey(e.target.value)}
            placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;...&#10;-----END RSA PRIVATE KEY-----"
            className="px-2 py-1.5 text-[11.5px] mono border border-border-soft rounded bg-bg min-h-[80px]"
            required
          />
        </Field>
        <Field label="Webhook secret" hint="The random string you used in the App's webhook config">
          <input
            data-testid="gh-webhook-secret"
            type="password"
            value={webhookSecret}
            onChange={(e) => setWebhookSecret(e.target.value)}
            className="px-2 py-1.5 text-[12.5px] mono border border-border-soft rounded bg-bg"
            required
          />
        </Field>
        <div className="flex gap-2 items-center">
          <Button
            type="submit"
            variant="primary"
            disabled={setCreds.isPending}
            data-testid="gh-save"
          >
            {setCreds.isPending ? "Saving…" : "Save credentials"}
          </Button>
          {setCreds.isSuccess && (
            <span className="text-success text-[12px]" data-testid="gh-saved">
              Saved.
            </span>
          )}
          {setCreds.isError && (
            <span className="text-danger text-[12px]">{(setCreds.error as Error).message}</span>
          )}
        </div>
      </form>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-text-2 text-[11.5px] font-medium">{label}</span>
      {children}
      {hint && <span className="text-text-4 text-[11px]">{hint}</span>}
    </div>
  );
}

// ─── Model API key ───────────────────────────────────────────────────────────

function ApiKeyCard() {
  // "configured" reflects whether the key is in the DB (the onboarding signal),
  // not whether the CLI is reachable. CLI reachability is shown separately in
  // the Plugin Health card below — overloading health here would make the badge
  // flip to "not set" the moment the `claude` binary is missing, even though
  // the user *did* set the key.
  const { data: onboarding } = useOnboarding();
  const setKey = useSetAnthropicKey();
  const [key, setKey_] = useState("");

  const configured = onboarding?.anthropic_key_set === true;

  return (
    <Card>
      <CardHeader>
        <Zap size={15} className="text-text-2" />
        <h2 className="font-semibold text-[13.5px]">Model API key</h2>
        <div className="flex-1" />
        <Badge variant={configured ? "success" : "danger"} data-testid="apikey-status">
          {configured ? "configured" : "not set"}
        </Badge>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-2.5">
          <p className="text-text-3 text-[12px]">
            {configured
              ? "Provider: Anthropic. Stored encrypted-at-rest. Re-enter to rotate."
              : "Anthropic key required. yaaos uses the Claude Code CLI internally — your key is encrypted at rest."}
          </p>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (key.trim()) {
                setKey.mutate(key, { onSuccess: () => setKey_("") });
              }
            }}
          >
            <input
              data-testid="anthropic-key"
              type="password"
              value={key}
              onChange={(e) => setKey_(e.target.value)}
              placeholder="sk-ant-..."
              className="flex-1 px-2 py-1.5 text-[12.5px] mono border border-border-soft rounded bg-bg"
            />
            <Button type="submit" disabled={setKey.isPending} data-testid="anthropic-save">
              Save
            </Button>
          </form>
          {setKey.isSuccess && (
            <div className="text-success text-[12px]" data-testid="anthropic-saved">
              Saved.
            </div>
          )}
          {setKey.isError && (
            <div className="text-danger text-[12px]">{(setKey.error as Error).message}</div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ─── Plugin health ───────────────────────────────────────────────────────────

function PluginHealthCard() {
  const { data: plugins, isLoading } = usePluginsList();
  return (
    <Card>
      <CardHeader>
        <Activity size={15} className="text-text-2" />
        <h2 className="font-semibold text-[13.5px]">Plugin health</h2>
        <div className="flex-1" />
        <span className="text-text-4 text-[11px] mono">auto-refresh 10s</span>
      </CardHeader>
      <div data-testid="plugin-health-list">
        {isLoading || !plugins ? (
          <div className="px-4 py-2.5 text-text-3 text-[12px]">Loading…</div>
        ) : plugins.length === 0 ? (
          <div className="px-4 py-2.5 text-text-3 text-[12px]">No plugins registered.</div>
        ) : (
          plugins.map((p, i) => <PluginHealthRow key={p.id} plugin={p} first={i === 0} />)
        )}
      </div>
    </Card>
  );
}

function PluginHealthRow({ plugin, first }: { plugin: PluginMeta; first: boolean }) {
  const { data, isLoading } = usePluginHealth(plugin.id);
  const border = first ? "" : "border-t border-border-soft";
  return (
    <div className={`flex items-center gap-4 px-4 py-3 ${border}`}>
      <div className="flex flex-col gap-0.5 min-w-[180px]">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-[13px]">{plugin.display_name}</span>
          <Badge variant="soft">{PLUGIN_TYPE_LABEL[plugin.type]}</Badge>
        </div>
        <span className="text-text-4 mono text-[10.5px]">{plugin.id}</span>
      </div>
      {isLoading || !data ? (
        <Badge variant="soft">checking</Badge>
      ) : (
        <Badge variant={data.healthy ? "success" : "danger"}>
          {data.healthy ? "healthy" : "unhealthy"}
        </Badge>
      )}
      <span className="text-text-3 text-[12px] flex-1 truncate">{data?.message ?? ""}</span>
      <span className="text-text-4 mono text-[11px]">checked {ago(data?.checked_at ?? null)}</span>
    </div>
  );
}
