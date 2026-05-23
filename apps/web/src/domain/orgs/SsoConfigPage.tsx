import { apiFetch } from "@core/api";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Checkbox } from "@shared/components/ui/checkbox";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { Textarea } from "@shared/components/ui/textarea";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

interface SsoConfig {
  enabled: boolean;
  jit_enabled: boolean;
  exempt_owner_user_id: string | null;
  updated_at?: string | null;
}

function useSsoConfig() {
  return useQuery<SsoConfig>({
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
  const { data, isLoading } = useSsoConfig();
  const upsert = useUpsertSsoConfig();

  const [metadata, setMetadata] = useState("");
  const [jit, setJit] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [exemptOwnerId, setExemptOwnerId] = useState("");

  if (isLoading) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="flex flex-col gap-4">
      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">SAML SSO — current state</h2>
        </header>
        <div className="px-4 py-4">
          <div className="text-sm flex items-center gap-2">
            <span>Enabled:</span>
            <Badge variant={data?.enabled ? "default" : "secondary"}>
              {data?.enabled ? "on" : "off"}
            </Badge>
            <span className="ml-4">JIT:</span>
            <Badge variant={data?.jit_enabled ? "default" : "secondary"}>
              {data?.jit_enabled ? "on" : "off"}
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
              upsert.mutate({
                idp_metadata_xml: metadata,
                jit_enabled: jit,
                enabled,
                exempt_owner_user_id: exemptOwnerId || null,
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
