import { PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
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
 * Org Settings > API Keys (BYOK). Lists every provider the backend's
 * validator registry exposes (M03 ships Anthropic only). Each provider gets
 * a card with reveal/test/save/clear and read-only last-validated /
 * last-used timestamps. The same `byok_keys` row is also surfaced on the
 * Claude Code settings page — both UIs round-trip through
 * `/api/api-keys/{provider}`.
 */
export function BYOKSettingsPage() {
  const providers = useByokProviders();
  return (
    <OrgSettingsLayout active="byok">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <PageHeader
          title="API Keys"
          subtitle="Bring your own LLM-provider keys. Encrypted at rest; never returned in plaintext after save."
        />
        {providers.isLoading && <p className="text-muted-foreground text-sm">Loading…</p>}
        {providers.isError && (
          <p className="text-sm text-destructive" data-testid="byok-load-err">
            Failed to load providers: {(providers.error as Error)?.message}
          </p>
        )}
        {(providers.data ?? []).length === 0 && !providers.isLoading && (
          <p className="text-muted-foreground text-sm" data-testid="byok-empty">
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
    <section
      className="rounded-lg border border-border bg-card"
      data-testid={`byok-card-${provider}`}
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold capitalize">{provider}</h3>
        {configured ? (
          <Badge data-testid={`byok-status-${provider}`}>configured</Badge>
        ) : (
          <Badge variant="destructive" data-testid={`byok-status-${provider}`}>
            not set
          </Badge>
        )}
      </header>
      <div className="px-4 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            type={reveal ? "text" : "password"}
            placeholder={configured ? "•••• replace value to update" : "Paste API key"}
            data-testid={`byok-input-${provider}`}
            className="flex-1 min-w-[200px]"
          />
          <Button
            variant="outline"
            size="sm"
            data-testid={`byok-reveal-${provider}`}
            onClick={() => setReveal((v) => !v)}
          >
            {reveal ? "Hide" : "Show"}
          </Button>
          <Button
            size="sm"
            data-testid={`byok-save-${provider}`}
            disabled={!value || setKey.isPending}
            onClick={onSave}
          >
            {setKey.isPending ? "Saving…" : "Save"}
          </Button>
          {configured && (
            <Button
              variant="outline"
              size="sm"
              data-testid={`byok-test-${provider}`}
              disabled={validate.isPending}
              onClick={() => validate.mutate(provider)}
            >
              {validate.isPending ? "Testing…" : "Test"}
            </Button>
          )}
          {configured && (
            <Button
              variant="destructive"
              size="sm"
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
            className={`mt-2 text-xs ${validate.data.valid ? "text-emerald-600" : "text-destructive"}`}
            data-testid={`byok-test-result-${provider}`}
          >
            {validate.data.valid ? "Key looks good." : "Key rejected."}
          </p>
        )}
        {setKey.isError && (
          <p className="mt-2 text-xs text-destructive" data-testid={`byok-save-err-${provider}`}>
            {(setKey.error as Error)?.message || "Failed"}
          </p>
        )}
        <Timestamps status={status} />
      </div>
    </section>
  );
}

function Timestamps({ status }: { status: ByokProviderStatus }) {
  const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
  return (
    <dl
      className="mt-4 grid grid-cols-3 gap-3 text-xs"
      data-testid={`byok-timestamps-${status.provider}`}
    >
      <div>
        <dt className="text-muted-foreground uppercase text-[10px] tracking-wide">
          Last validated
        </dt>
        <dd className="font-mono mt-0.5">{fmt(status.last_validated_at)}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground uppercase text-[10px] tracking-wide">Last used</dt>
        <dd className="font-mono mt-0.5">{fmt(status.last_used_at)}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground uppercase text-[10px] tracking-wide">Updated</dt>
        <dd className="font-mono mt-0.5">{fmt(status.updated_at)}</dd>
      </div>
    </dl>
  );
}
