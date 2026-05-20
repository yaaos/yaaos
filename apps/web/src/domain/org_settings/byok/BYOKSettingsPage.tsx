import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
import { useState } from "react";
import { OrgSettingsLayout } from "../OrgSettingsLayout";
import {
  type ByokProviderStatus,
  useByokProviders,
  useClearByok,
  useSetByok,
  useValidateByok,
} from "./queries";

/**
 * Org Settings > BYOK. Lists every provider the backend's validator
 * registry exposes (M03 ships Anthropic only). Each provider gets a card
 * with reveal/test/save/clear and read-only last-validated / last-used
 * timestamps. The same `byok_keys` row is also surfaced on the Claude Code
 * settings page — both UIs round-trip through `/api/byok/{provider}`.
 */
export function BYOKSettingsPage() {
  const providers = useByokProviders();
  return (
    <OrgSettingsLayout active="byok">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <h2 className="text-[16px] font-semibold">BYOK</h2>
        <p className="text-text-3 text-sm">
          Bring your own API keys for the LLM providers yaaos uses. Keys are encrypted at rest and
          never returned to the UI in plaintext after save.
        </p>
        {providers.isLoading && <p className="text-text-3 text-sm">Loading…</p>}
        {providers.isError && (
          <p className="text-sm text-red-500" data-testid="byok-load-err">
            Failed to load providers: {(providers.error as Error)?.message}
          </p>
        )}
        {(providers.data ?? []).length === 0 && !providers.isLoading && (
          <p className="text-text-3 text-sm" data-testid="byok-empty">
            No BYOK-capable providers registered. Install a provider plugin to surface one here.
          </p>
        )}
        {(providers.data ?? []).map((p) => (
          <ProviderCard key={p.provider} status={p} />
        ))}
      </div>
    </OrgSettingsLayout>
  );
}

function ProviderCard({ status }: { status: ByokProviderStatus }) {
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const setKey = useSetByok();
  const validate = useValidateByok();
  const clear = useClearByok();

  const configured = status.status === "configured";
  const provider = status.provider;

  const onSave = () => {
    if (!value) return;
    setKey.mutate({ provider, value }, { onSuccess: () => setValue("") });
  };

  return (
    <Card data-testid={`byok-card-${provider}`}>
      <CardHeader>
        <div className="flex items-center gap-2">
          <h3 className="text-[13.5px] font-semibold capitalize">{provider}</h3>
          {configured ? (
            <Badge variant="success" data-testid={`byok-status-${provider}`}>
              configured
            </Badge>
          ) : (
            <Badge variant="danger" data-testid={`byok-status-${provider}`}>
              not set
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-2">
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            type={reveal ? "text" : "password"}
            placeholder={configured ? "•••• replace value to update" : "Paste API key"}
            data-testid={`byok-input-${provider}`}
            className="flex-1 rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
          />
          <Button data-testid={`byok-reveal-${provider}`} onClick={() => setReveal((v) => !v)}>
            {reveal ? "Hide" : "Show"}
          </Button>
          <Button
            data-testid={`byok-save-${provider}`}
            disabled={!value || setKey.isPending}
            onClick={onSave}
          >
            {setKey.isPending ? "Saving…" : "Save"}
          </Button>
          {configured && (
            <Button
              data-testid={`byok-test-${provider}`}
              disabled={validate.isPending}
              onClick={() => validate.mutate(provider)}
            >
              {validate.isPending ? "Testing…" : "Test"}
            </Button>
          )}
          {configured && (
            <Button
              data-testid={`byok-clear-${provider}`}
              disabled={clear.isPending}
              onClick={() => clear.mutate(provider)}
            >
              Remove
            </Button>
          )}
        </div>
        {validate.data && validate.variables === provider && (
          <p
            className={`mt-2 text-xs ${validate.data.valid ? "text-success" : "text-red-500"}`}
            data-testid={`byok-test-result-${provider}`}
          >
            {validate.data.valid ? "Key looks good." : "Key rejected."}
          </p>
        )}
        {setKey.isError && (
          <p className="mt-2 text-xs text-red-500" data-testid={`byok-save-err-${provider}`}>
            {(setKey.error as Error)?.message || "Failed"}
          </p>
        )}
        <Timestamps status={status} />
      </CardContent>
    </Card>
  );
}

function Timestamps({ status }: { status: ByokProviderStatus }) {
  const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
  return (
    <div
      className="text-text-4 mt-3 grid grid-cols-3 gap-2 text-[10.5px]"
      data-testid={`byok-timestamps-${status.provider}`}
    >
      <div>
        <div className="uppercase">Last validated</div>
        <div className="mono">{fmt(status.last_validated_at)}</div>
      </div>
      <div>
        <div className="uppercase">Last used</div>
        <div className="mono">{fmt(status.last_used_at)}</div>
      </div>
      <div>
        <div className="uppercase">Updated</div>
        <div className="mono">{fmt(status.updated_at)}</div>
      </div>
    </div>
  );
}
