import { apiFetch } from "@core/api";
import { ErrorBanner } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Checkbox } from "@shared/components/ui/checkbox";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Textarea } from "@shared/components/ui/textarea";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";

interface SsoConfig {
  enabled: boolean;
  jit_enabled: boolean;
  exempt_owner_user_id: string | null;
  email_domains: string[];
  updated_at?: string | null;
}

function useSsoConfig() {
  return useSuspenseQuery<SsoConfig>({
    queryKey: ["sso", "config"],
    queryFn: () => apiFetch<SsoConfig>("/api/sso/config"),
  });
}

function useUpsertSsoConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      idp_metadata_xml: string;
      jit_enabled: boolean;
      enabled: boolean;
      exempt_owner_user_id: string | null;
      email_domains: string[];
    }) =>
      apiFetch<SsoConfig>("/api/sso/config", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sso", "config"] }),
  });
}

/**
 * Owner-only SSO config page. Lets the operator paste IdP metadata XML,
 * toggle JIT, and pick an exempt Owner (only Owners with a verified TOTP
 * secret are accepted server-side). Hand the IdP the `/api/sso/<slug>/metadata`
 * URL to register yaaos as the SP.
 */
export function SsoConfigPage() {
  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load SSO config." onRetry={resetErrorBoundary} />
      )}
    >
      <Suspense fallback={<Skeleton className="h-32 m-6" />}>
        <SsoConfigContent />
      </Suspense>
    </ErrorBoundary>
  );
}

function SsoConfigContent() {
  const { data } = useSsoConfig();
  const upsert = useUpsertSsoConfig();

  const [metadata, setMetadata] = useState("");
  const [jit, setJit] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [exemptOwnerId, setExemptOwnerId] = useState("");
  // Comma- or newline-separated free-text editor; we normalize to a list
  // on submit. Pre-populated from server state once it loads.
  const [domainsRaw, setDomainsRaw] = useState<string>((data?.email_domains ?? []).join(", "));

  return (
    <div className="flex flex-col gap-4">
      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">SAML SSO — current state</h2>
        </header>
        <div className="px-4 py-4">
          <div className="text-sm flex items-center gap-2">
            <span>Enabled:</span>
            <Badge variant={data.enabled ? "default" : "secondary"}>
              {data.enabled ? "on" : "off"}
            </Badge>
            <span className="ml-4">JIT:</span>
            <Badge variant={data.jit_enabled ? "default" : "secondary"}>
              {data.jit_enabled ? "on" : "off"}
            </Badge>
          </div>
          <p className="text-muted-foreground text-xs mt-2">
            Hand the IdP this URL as the SP entity / ACS:{" "}
            <code className="font-mono">/api/sso/&lt;your-slug&gt;/metadata</code>
          </p>
        </div>
      </section>

      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Update config</h2>
        </header>
        <div className="px-4 py-4">
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              const email_domains = domainsRaw
                .split(/[,\n]/)
                .map((d) => d.trim().toLowerCase())
                .filter(Boolean);
              upsert.mutate({
                idp_metadata_xml: metadata,
                jit_enabled: jit,
                enabled,
                exempt_owner_user_id: exemptOwnerId || null,
                email_domains,
              });
            }}
          >
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sso-metadata">IdP metadata XML</Label>
              <Textarea
                id="sso-metadata"
                value={metadata}
                onChange={(e) => setMetadata(e.target.value)}
                className="font-mono text-xs"
                rows={8}
                placeholder="<EntityDescriptor>...</EntityDescriptor>"
                required
              />
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Checkbox
                id="sso-enabled"
                checked={enabled}
                onCheckedChange={(v) => setEnabled(v === true)}
              />
              <Label htmlFor="sso-enabled">Enable SSO for this org</Label>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Checkbox id="sso-jit" checked={jit} onCheckedChange={(v) => setJit(v === true)} />
              <Label htmlFor="sso-jit">JIT-create memberships on first SSO login</Label>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sso-exempt">Exempt Owner user id (must have verified 2FA)</Label>
              <Input
                id="sso-exempt"
                value={exemptOwnerId}
                onChange={(e) => setExemptOwnerId(e.target.value)}
                className="font-mono text-xs"
                placeholder="(none)"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sso-domains">Email domain claims (comma or newline separated)</Label>
              <Textarea
                id="sso-domains"
                data-testid="sso-domains"
                value={domainsRaw}
                onChange={(e) => setDomainsRaw(e.target.value)}
                className="font-mono text-xs"
                rows={2}
                placeholder="acme.com, partner.example.com"
              />
              <p className="text-muted-foreground text-xs">
                Logging in with an email matching one of these domains routes the user through this
                org's SSO. Lowercase, no `@`, no globs.
              </p>
            </div>
            <div>
              <Button type="submit" disabled={upsert.isPending} data-testid="sso-save">
                {upsert.isPending ? "Saving…" : "Save"}
              </Button>
            </div>
            {upsert.isError && (
              <p className="text-destructive text-xs">
                {(upsert.error as Error)?.message ?? "Save failed"}
              </p>
            )}
          </form>
        </div>
      </section>
    </div>
  );
}
