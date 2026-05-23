import { PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Checkbox } from "@shared/components/ui/checkbox";
import { Input } from "@shared/components/ui/input";
import { useState } from "react";
import { OrgSettingsLayout } from "../OrgSettingsLayout";
import {
  type IntegrationStatus,
  useDeleteIntegration,
  useIntegrations,
  usePatchIntegration,
  useValidateIntegration,
} from "./queries";

/**
 * Org Settings > MCP Proxy (Integrations).
 *
 * One card per registered provider. Empty state offers a Connect button that
 * starts the upstream OAuth handshake; connected state shows the upstream
 * identity, last-validated timestamp, allowlist editor, enabled toggle, and
 * Reconnect / Disconnect controls. A red Reconnect-required badge appears
 * whenever `last_refresh_status="failed"` (status="broken").
 */
export function IntegrationsSettingsPage() {
  const { data, isLoading, error } = useIntegrations();
  return (
    <OrgSettingsLayout active="mcp-proxy">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <PageHeader
          title="MCP Proxy"
          subtitle="Connect Linear and Notion so the reviewer agent can pull issue and document context via MCP. Dedicate a bot user per provider so reviews never act as a human teammate."
        />
        {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
        {error && (
          <p className="text-sm text-destructive" data-testid="integrations-err">
            {(error as Error).message}
          </p>
        )}
        {(data ?? []).map((p) => (
          <ProviderCard key={p.provider} provider={p} />
        ))}
      </div>
    </OrgSettingsLayout>
  );
}

function ProviderCard({ provider }: { provider: IntegrationStatus }) {
  const patch = usePatchIntegration();
  const del = useDeleteIntegration();
  const validate = useValidateIntegration();
  const [confirming, setConfirming] = useState(false);

  const connectUrl = `/api/integrations/${provider.provider}/connect`;

  return (
    <section
      className="rounded-lg border border-border bg-card"
      data-testid={`integration-card-${provider.provider}`}
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold capitalize">{provider.provider}</h3>
        <StatusBadge provider={provider} />
      </header>
      <div className="px-4 py-4">
        {provider.status === "not_set" ? (
          <EmptyState provider={provider.provider} connectUrl={connectUrl} />
        ) : (
          <ConnectedState
            provider={provider}
            connectUrl={connectUrl}
            confirming={confirming}
            setConfirming={setConfirming}
            patch={patch}
            del={del}
            validate={validate}
          />
        )}
      </div>
    </section>
  );
}

function StatusBadge({ provider }: { provider: IntegrationStatus }) {
  if (provider.status === "not_set") {
    return (
      <Badge variant="secondary" data-testid={`badge-${provider.provider}-disconnected`}>
        Disconnected
      </Badge>
    );
  }
  if (provider.status === "broken") {
    return (
      <Badge variant="destructive" data-testid={`badge-${provider.provider}-broken`}>
        Reconnect required
      </Badge>
    );
  }
  return <Badge data-testid={`badge-${provider.provider}-connected`}>Connected</Badge>;
}

function EmptyState({ provider, connectUrl }: { provider: string; connectUrl: string }) {
  return (
    <div className="text-sm">
      <p className="text-muted-foreground mb-3 text-xs">
        Connect a dedicated {provider} bot user (recommended) so reviews never run as a human
        teammate.
      </p>
      <Button asChild size="sm">
        <a href={connectUrl} data-testid={`connect-${provider}`}>
          Connect
        </a>
      </Button>
    </div>
  );
}

function ConnectedState({
  provider,
  connectUrl,
  confirming,
  setConfirming,
  patch,
  del,
  validate,
}: {
  provider: IntegrationStatus;
  connectUrl: string;
  confirming: boolean;
  setConfirming: (v: boolean) => void;
  patch: ReturnType<typeof usePatchIntegration>;
  del: ReturnType<typeof useDeleteIntegration>;
  validate: ReturnType<typeof useValidateIntegration>;
}) {
  const enabled = provider.enabled !== false;
  const onToggleEnabled = () => {
    patch.mutate({ provider: provider.provider, body: { enabled: !enabled } });
  };
  const onDisconnect = () => {
    setConfirming(false);
    del.mutate(provider.provider);
  };
  const onValidate = () => {
    validate.mutate(provider.provider);
  };

  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="text-xs text-muted-foreground">
        <p>
          Connected as <span className="font-mono">{provider.upstream_identity ?? "—"}</span>
        </p>
        {provider.last_validated_at && (
          <p>
            Last validated <span className="font-mono">{provider.last_validated_at}</span>
          </p>
        )}
        {provider.last_refresh_failed_at && (
          <p className="text-destructive">
            Last failure <span className="font-mono">{provider.last_refresh_failed_at}</span>
          </p>
        )}
      </div>

      <AllowlistEditor provider={provider} patch={patch} />

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2 text-xs">
          <Checkbox
            id={`enabled-${provider.provider}`}
            checked={enabled}
            onCheckedChange={onToggleEnabled}
            data-testid={`enabled-${provider.provider}`}
            disabled={patch.isPending}
          />
          <label htmlFor={`enabled-${provider.provider}`}>Enabled</label>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={onValidate}
          disabled={validate.isPending}
          data-testid={`test-${provider.provider}`}
        >
          {validate.isPending ? "Testing…" : "Test connection"}
        </Button>
        {validate.isSuccess && (
          <span
            className={
              validate.data?.valid ? "text-xs text-emerald-600" : "text-xs text-destructive"
            }
            data-testid={`test-result-${provider.provider}`}
          >
            {validate.data?.valid ? "OK" : "Failed"}
          </span>
        )}
        <Button asChild variant="outline" size="sm">
          <a href={connectUrl} data-testid={`reconnect-${provider.provider}`}>
            Reconnect
          </a>
        </Button>
        <Button
          variant="destructive"
          size="sm"
          data-testid={`disconnect-${provider.provider}`}
          onClick={() => setConfirming(true)}
          disabled={del.isPending}
        >
          Disconnect
        </Button>
      </div>
      {confirming && (
        <div
          className="rounded-md border border-border bg-muted/50 p-3"
          data-testid={`disconnect-confirm-${provider.provider}`}
        >
          <p className="mb-2 text-xs">
            Disconnect {provider.provider}? Reviews will receive `not_connected` errors when calling
            its tools.
          </p>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              data-testid={`disconnect-confirm-btn-${provider.provider}`}
              onClick={onDisconnect}
            >
              Disconnect
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function AllowlistEditor({
  provider,
  patch,
}: {
  provider: IntegrationStatus;
  patch: ReturnType<typeof usePatchIntegration>;
}) {
  const allowed = provider.allowed_tools;
  const [draft, setDraft] = useState("");

  const onRemove = (tool: string) => {
    patch.mutate({
      provider: provider.provider,
      body: { allowed_tools: allowed.filter((t) => t !== tool) },
    });
  };
  const onAdd = () => {
    const t = draft.trim();
    if (!t || allowed.includes(t)) return;
    patch.mutate({
      provider: provider.provider,
      body: { allowed_tools: [...allowed, t] },
    });
    setDraft("");
  };

  return (
    <div className="rounded-md border border-border bg-muted/50 p-3">
      <p className="mb-2 text-xs text-muted-foreground">
        Write-tool allowlist — only listed tools may be called. Read tools are always allowed.
      </p>
      <div className="mb-2 flex flex-wrap gap-1" data-testid={`allowlist-${provider.provider}`}>
        {allowed.length === 0 && <span className="text-xs text-muted-foreground">(none)</span>}
        {allowed.map((tool) => (
          <span
            key={tool}
            className="inline-flex items-center gap-1 rounded bg-background px-2 py-0.5 text-xs border border-border"
            data-testid={`allow-chip-${provider.provider}-${tool}`}
          >
            <span className="font-mono">{tool}</span>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground"
              onClick={() => onRemove(tool)}
              data-testid={`allow-remove-${provider.provider}-${tool}`}
              aria-label={`Remove ${tool}`}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="write_tool_name"
          data-testid={`allow-input-${provider.provider}`}
          className="h-8 flex-1"
        />
        <Button
          size="sm"
          variant="outline"
          onClick={onAdd}
          disabled={!draft.trim() || patch.isPending}
          data-testid={`allow-add-${provider.provider}`}
        >
          Add
        </Button>
      </div>
    </div>
  );
}
