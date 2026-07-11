import { zodResolver } from "@hookform/resolvers/zod";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { OrgSettingsLayout } from "@shared/components/public/layout/org-settings-layout";
import { PageHeader } from "@shared/components/public/layout/page-header";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Form, FormControl, FormField, FormItem, FormMessage } from "@shared/components/ui/form";
import { Input } from "@shared/components/ui/input";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { useForm } from "react-hook-form";
import { z } from "zod";
import {
  type ApiKeyProviderStatus,
  useApiKeyProviders,
  useClearApiKey,
  useSetApiKey,
  useValidateApiKey,
} from "../../api_keys/queries";

/**
 * Org Settings > API Keys. Lists every provider the backend's
 * validator registry exposes (ships Anthropic only). Each provider
 * card is write-only: once a key is configured, the input is hidden
 * behind a Rotate button — the plaintext is never read back from the
 * backend, so we don't pretend it is. Test/Rotate/Clear actions surface
 * the underlying timestamps. The same `org_api_keys` row is also shown on
 * the Claude Code settings page; both round-trip through
 * `/api/api-keys/{provider}`.
 */
export function ApiKeysSettingsPage() {
  return (
    <OrgSettingsLayout active="api-keys">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <PageHeader
          title="API Keys"
          subtitle="API keys for external providers. Encrypted at rest; never returned in plaintext after save."
        />
        <ErrorBoundary
          fallbackRender={({ resetErrorBoundary }) => (
            <ErrorBanner message="Couldn't load API key providers." onRetry={resetErrorBoundary} />
          )}
        >
          <Suspense fallback={<Skeleton className="h-24" />}>
            <ApiKeyProviderList />
          </Suspense>
        </ErrorBoundary>
      </div>
    </OrgSettingsLayout>
  );
}

function ApiKeyProviderList() {
  const { data: providers } = useApiKeyProviders();
  if (providers.length === 0) {
    return (
      <p className="text-muted-foreground text-sm" data-testid="apikey-empty">
        No API key providers registered. Install a provider plugin to surface one here.
      </p>
    );
  }
  return (
    <>
      {providers.map((p) => (
        <ProviderCard key={p.provider} status={p} />
      ))}
    </>
  );
}

const apiKeySchema = z.object({
  value: z.string().min(1, "API key is required."),
});

type ApiKeyValues = z.infer<typeof apiKeySchema>;

function ProviderCard({ status }: { status: ApiKeyProviderStatus }) {
  // Editing mode: shown when the key isn't set, or when the user clicks Rotate.
  const [editing, setEditing] = useState(status.status !== "configured");
  const setKey = useSetApiKey();
  const validate = useValidateApiKey();
  const clear = useClearApiKey();

  const configured = status.status === "configured";
  const provider = status.provider;

  const form = useForm<ApiKeyValues>({
    resolver: zodResolver(apiKeySchema),
    defaultValues: { value: "" },
  });

  const onSave = (values: ApiKeyValues) => {
    setKey.mutate(
      { provider, value: values.value },
      {
        onSuccess: () => {
          form.reset();
          setEditing(false);
        },
      },
    );
  };

  const onCancelRotate = () => {
    form.reset();
    setEditing(false);
  };

  return (
    <section
      className="rounded-lg border border-border bg-card"
      data-testid={`apikey-card-${provider}`}
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold capitalize">{provider}</h3>
        {configured ? (
          <Badge data-testid={`apikey-status-${provider}`}>configured</Badge>
        ) : (
          <Badge variant="destructive" data-testid={`apikey-status-${provider}`}>
            not set
          </Badge>
        )}
      </header>
      <div className="px-4 py-4">
        {!editing && configured && (
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="text-sm text-muted-foreground"
              data-testid={`apikey-configured-summary-${provider}`}
            >
              Configured ✓ · last set{" "}
              {status.updated_at ? new Date(status.updated_at).toLocaleString() : "—"}
            </span>
            <Button
              variant="outline"
              size="sm"
              data-testid={`apikey-test-${provider}`}
              disabled={validate.isPending}
              onClick={() => validate.mutate(provider)}
            >
              {validate.isPending ? "Testing…" : "Test"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              data-testid={`apikey-rotate-${provider}`}
              onClick={() => setEditing(true)}
            >
              Rotate
            </Button>
            <Button
              variant="destructive"
              size="sm"
              data-testid={`apikey-clear-${provider}`}
              disabled={clear.isPending}
              onClick={() => clear.mutate(provider)}
            >
              Clear
            </Button>
          </div>
        )}
        {editing && (
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSave)} className="flex flex-wrap items-start gap-2">
              <FormField
                control={form.control}
                name="value"
                render={({ field }) => (
                  <FormItem className="flex-1 min-w-[200px]">
                    <FormControl>
                      <Input
                        {...field}
                        type="password"
                        placeholder={configured ? "Paste new API key to replace" : "Paste API key"}
                        data-testid={`apikey-input-${provider}`}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button
                type="submit"
                size="sm"
                data-testid={`apikey-save-${provider}`}
                disabled={!form.watch("value") || setKey.isPending}
              >
                {setKey.isPending ? "Saving…" : "Save"}
              </Button>
              {configured && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  data-testid={`apikey-rotate-cancel-${provider}`}
                  onClick={onCancelRotate}
                >
                  Cancel
                </Button>
              )}
            </form>
          </Form>
        )}
        {validate.data && validate.variables === provider && (
          <p
            className={`mt-2 text-xs ${validate.data.valid ? "text-emerald-600" : "text-destructive"}`}
            data-testid={`apikey-test-result-${provider}`}
          >
            {validate.data.valid ? "Key looks good." : "Key rejected."}
          </p>
        )}
        {setKey.isError && (
          <p className="mt-2 text-xs text-destructive" data-testid={`apikey-save-err-${provider}`}>
            {(setKey.error as Error)?.message || "Failed"}
          </p>
        )}
        <Timestamps status={status} />
      </div>
    </section>
  );
}

function Timestamps({ status }: { status: ApiKeyProviderStatus }) {
  const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
  return (
    <dl
      className="mt-4 grid grid-cols-3 gap-3 text-xs"
      data-testid={`apikey-timestamps-${status.provider}`}
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
