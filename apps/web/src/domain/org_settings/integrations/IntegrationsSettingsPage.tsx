import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
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
 * Org Settings > Integrations.
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
    <OrgSettingsLayout active="integrations">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <h2 className="text-[16px] font-semibold">Integrations</h2>
        <p className="text-xs text-text-3">
          Connect Linear and Notion so the reviewer agent can pull issue and document context via
          MCP. yaaos recommends a dedicated bot user per provider so reviews never act as a human
          teammate.
        </p>
        {isLoading && <p className="text-sm text-text-3">Loading…</p>}
        {error && (
          <p className="text-sm text-red-500" data-testid="integrations-err">
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
    <Card data-testid={`integration-card-${provider.provider}`}>
      <CardHeader>
        <div className="flex items-center gap-2">
          <h3 className="text-[13.5px] font-semibold capitalize">{provider.provider}</h3>
          <StatusBadge provider={provider} />
        </div>
      </CardHeader>
      <CardContent>
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
      </CardContent>
    </Card>
  );
}

function StatusBadge({ provider }: { provider: IntegrationStatus }) {
  if (provider.status === "not_set") {
    return <Badge data-testid={`badge-${provider.provider}-disconnected`}>Disconnected</Badge>;
  }
  if (provider.status === "broken") {
    return (
      <Badge variant="danger" data-testid={`badge-${provider.provider}-broken`}>
        Reconnect required
      </Badge>
    );
  }
  return (
    <Badge variant="success" data-testid={`badge-${provider.provider}-connected`}>
      Connected
    </Badge>
  );
}

function EmptyState({ provider, connectUrl }: { provider: string; connectUrl: string }) {
  return (
    <div className="text-sm">
      <p className="text-text-3 mb-3 text-xs">
        Connect a dedicated {provider} bot user (recommended) so reviews never run as a human
        teammate.
      </p>
      <a
        href={connectUrl}
        data-testid={`connect-${provider}`}
        className="inline-flex items-center rounded border border-border-soft px-3 py-1.5 text-xs hover:bg-hover"
      >
        Connect
      </a>
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
      <div className="text-xs text-text-3">
        <p>
          Connected as <span className="font-mono">{provider.upstream_identity ?? "—"}</span>
        </p>
        {provider.last_validated_at && (
          <p>
            Last validated <span className="font-mono">{provider.last_validated_at}</span>
          </p>
        )}
        {provider.last_refresh_failed_at && (
          <p className="text-red-600">
            Last failure <span className="font-mono">{provider.last_refresh_failed_at}</span>
          </p>
        )}
      </div>

      <AllowlistEditor provider={provider} patch={patch} />

      <div className="flex items-center gap-2">
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={enabled}
            onChange={onToggleEnabled}
            data-testid={`enabled-${provider.provider}`}
            disabled={patch.isPending}
          />
          <span>Enabled</span>
        </label>
        <button
          type="button"
          onClick={onValidate}
          disabled={validate.isPending}
          data-testid={`test-${provider.provider}`}
          className="rounded border border-border-soft px-3 py-1.5 text-xs hover:bg-hover"
        >
          {validate.isPending ? "Testing…" : "Test connection"}
        </button>
        {validate.isSuccess && (
          <span
            className={validate.data?.valid ? "text-xs text-success" : "text-xs text-red-500"}
            data-testid={`test-result-${provider.provider}`}
          >
            {validate.data?.valid ? "OK" : "Failed"}
          </span>
        )}
        <a
          href={connectUrl}
          data-testid={`reconnect-${provider.provider}`}
          className="rounded border border-border-soft px-3 py-1.5 text-xs hover:bg-hover"
        >
          Reconnect
        </a>
        <Button
          data-testid={`disconnect-${provider.provider}`}
          onClick={() => setConfirming(true)}
          disabled={del.isPending}
        >
          Disconnect
        </Button>
      </div>
      {confirming && (
        <div
          className="rounded border border-border-soft bg-bg-2 p-3"
          data-testid={`disconnect-confirm-${provider.provider}`}
        >
          <p className="mb-2 text-xs">
            Disconnect {provider.provider}? Reviews will receive `not_connected` errors when calling
            its tools.
          </p>
          <div className="flex gap-2">
            <Button onClick={() => setConfirming(false)}>Cancel</Button>
            <Button
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
  // Phase 4 ships allowlist editor for any provider whose `mcp_credentials.allowed_tools`
  // is mutable. The provider's full known-write-tools catalogue lives on the
  // backend; for Phase 4 we surface the row's current allowed_tools as
  // toggleable chips + a free-text add box. Provider-specific known-write-tools
  // discovery lands with Phase 5's e2e — for now operators see whatever is
  // there and can clear / add.
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
    <div className="rounded border border-border-soft bg-bg-2 p-3">
      <p className="mb-2 text-xs text-text-3">
        Write-tool allowlist — only listed tools may be called. Read tools are always allowed.
      </p>
      <div className="mb-2 flex flex-wrap gap-1" data-testid={`allowlist-${provider.provider}`}>
        {allowed.length === 0 && <span className="text-xs text-text-3">(none)</span>}
        {allowed.map((tool) => (
          <span
            key={tool}
            className="inline-flex items-center gap-1 rounded bg-bg px-2 py-0.5 text-xs"
            data-testid={`allow-chip-${provider.provider}-${tool}`}
          >
            <span className="font-mono">{tool}</span>
            <button
              type="button"
              className="text-text-3 hover:text-text"
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
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="write_tool_name"
          data-testid={`allow-input-${provider.provider}`}
          className="flex-1 rounded border border-border-soft bg-bg px-2 py-1 text-xs"
        />
        <button
          type="button"
          onClick={onAdd}
          disabled={!draft.trim() || patch.isPending}
          data-testid={`allow-add-${provider.provider}`}
          className="rounded border border-border-soft px-3 py-1 text-xs hover:bg-hover"
        >
          Add
        </button>
      </div>
    </div>
  );
}
