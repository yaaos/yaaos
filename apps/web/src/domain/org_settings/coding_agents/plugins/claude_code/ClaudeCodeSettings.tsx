import { useMembership } from "@core/api/public/membership";
import { useCurrentOrgSlug } from "@core/api/public/org-context";
import { useBrokenSummary } from "@core/api/public/queries";
import { zodResolver } from "@hookform/resolvers/zod";
import { ConfirmModal } from "@shared/components/public/layout/confirm-modal";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Form, FormControl, FormField, FormItem, FormMessage } from "@shared/components/ui/form";
import { Input } from "@shared/components/ui/input";
import { Suspense, useEffect, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { type CodingAgentInstall, useCodingAgents, useUninstallCodingAgent } from "../../queries";
import {
  useApiKeyAnthropicStatus,
  useClearApiKeyAnthropic,
  useSetApiKeyAnthropic,
  useValidateApiKeyAnthropic,
} from "./queries";

/**
 * Bespoke settings UI for the `claude_code` coding-agent plugin.
 *
 * Renders:
 *  - Anthropic API key card (provider=anthropic — test/save/rotate/clear).
 *  - Danger zone (uninstall).
 */
export function ClaudeCodeSettings({ pluginId }: { pluginId: string }) {
  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load Claude Code settings." onRetry={resetErrorBoundary} />
      )}
    >
      <Suspense fallback={<p className="text-muted-foreground p-4 text-sm">Loading…</p>}>
        <ClaudeCodeContent pluginId={pluginId} />
      </Suspense>
    </ErrorBoundary>
  );
}

function ClaudeCodeContent({ pluginId }: { pluginId: string }) {
  const { data: installs } = useCodingAgents();

  const install = installs.find((i) => i.plugin_id === pluginId);

  if (!install) {
    return (
      <section className="rounded-lg border border-border bg-card">
        <div className="px-4 py-4">
          <p className="text-muted-foreground text-sm" data-testid="cc-not-installed">
            Claude Code is not installed for this org. Install it from the Coding Agents page first.
          </p>
        </div>
      </section>
    );
  }

  return <Editor install={install} />;
}

// ── Editor ───────────────────────────────────────────────────────────────────

function Editor({ install }: { install: CodingAgentInstall }) {
  return (
    <div className="flex flex-col gap-4">
      <BrokenIntegrationsNotice />
      <BuilderReadOnlyBanner />
      <AnthropicKeyCard />
      <DangerZone pluginId={install.plugin_id} />
    </div>
  );
}

// ── Anthropic key card ───────────────────────────────────────────────────────

const anthropicKeySchema = z.object({
  value: z.string().min(1, "API key is required."),
});

type AnthropicKeyValues = z.infer<typeof anthropicKeySchema>;

function AnthropicKeyCard() {
  const status = useApiKeyAnthropicStatus();
  const setKey = useSetApiKeyAnthropic();
  const validate = useValidateApiKeyAnthropic();
  const clear = useClearApiKeyAnthropic();
  const configured = status.data?.status === "configured";
  const [editing, setEditing] = useState(!configured);

  // Sync editing state when status finishes loading (initial render runs with
  // configured=undefined → editing=true; once status arrives we close the
  // input if a key is already set).
  useEffect(() => {
    if (status.data && configured) setEditing(false);
  }, [status.data, configured]);

  const keyForm = useForm<AnthropicKeyValues>({
    resolver: zodResolver(anthropicKeySchema),
    defaultValues: { value: "" },
  });

  const onSaveKey = (values: AnthropicKeyValues) => {
    setKey.mutate(values.value, {
      onSuccess: () => {
        keyForm.reset();
        setEditing(false);
      },
    });
  };

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <h3 className="text-[13.5px] font-semibold">Anthropic API key</h3>
          {configured ? (
            <Badge variant="default" data-testid="cc-key-configured">
              configured
            </Badge>
          ) : (
            <Badge variant="destructive" data-testid="cc-key-not-set">
              not set
            </Badge>
          )}
        </div>
      </header>
      <div className="px-4 py-4">
        {!editing && configured && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground" data-testid="cc-key-summary">
              Configured ✓ · last set{" "}
              {status.data?.updated_at ? new Date(status.data.updated_at).toLocaleString() : "—"}
            </span>
            <Button
              type="button"
              data-testid="cc-key-test"
              disabled={validate.isPending}
              onClick={() => validate.mutate()}
            >
              {validate.isPending ? "Testing…" : "Test"}
            </Button>
            <Button type="button" data-testid="cc-key-rotate" onClick={() => setEditing(true)}>
              Rotate
            </Button>
            <Button
              type="button"
              data-testid="cc-key-clear"
              disabled={clear.isPending}
              onClick={() => clear.mutate()}
            >
              Clear
            </Button>
          </div>
        )}
        {editing && (
          <Form {...keyForm}>
            <form onSubmit={keyForm.handleSubmit(onSaveKey)} className="flex items-start gap-2">
              <FormField
                control={keyForm.control}
                name="value"
                render={({ field }) => (
                  <FormItem className="flex-1">
                    <FormControl>
                      <Input
                        {...field}
                        type="password"
                        placeholder={configured ? "Paste new API key to replace" : "sk-ant-..."}
                        data-testid="cc-key-input"
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button type="submit" data-testid="cc-key-save" disabled={setKey.isPending}>
                {setKey.isPending ? "Saving…" : "Save"}
              </Button>
              {configured && (
                <Button
                  type="button"
                  data-testid="cc-key-rotate-cancel"
                  onClick={() => {
                    keyForm.reset();
                    setEditing(false);
                  }}
                >
                  Cancel
                </Button>
              )}
            </form>
          </Form>
        )}
        {validate.data && (
          <p
            className={`mt-2 text-xs ${validate.data.valid ? "text-emerald-600" : "text-destructive"}`}
            data-testid="cc-key-test-result"
          >
            {validate.data.valid ? "Key looks good." : "Key rejected."}
          </p>
        )}
      </div>
    </section>
  );
}

// ── Sub-components ───────────────────────────────────────────────────────────

function DangerZone({ pluginId }: { pluginId: string }) {
  const uninstall = useUninstallCodingAgent();
  const [showConfirm, setShowConfirm] = useState(false);
  return (
    <>
      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h3 className="text-[13.5px] font-semibold text-destructive">Danger zone</h3>
        </header>
        <div className="px-4 py-4">
          <div className="flex items-start justify-between gap-4">
            <p className="text-muted-foreground text-sm">
              Uninstall Claude Code from this org. Existing reviews keep their findings, but future
              PRs in this org won't be reviewed until a coding agent is reinstalled.
            </p>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setShowConfirm(true)}
              disabled={uninstall.isPending}
              data-testid="cc-uninstall-button"
              className="text-destructive"
            >
              Uninstall
            </Button>
          </div>
        </div>
      </section>
      <ConfirmModal
        open={showConfirm}
        onOpenChange={setShowConfirm}
        title="Uninstall Claude Code?"
        body="The plugin configuration will be removed permanently. This cannot be undone."
        confirmLabel="Uninstall"
        tone="destructive"
        pending={uninstall.isPending}
        onConfirm={() => {
          uninstall.mutate(pluginId, { onSettled: () => setShowConfirm(false) });
        }}
      />
    </>
  );
}

/** Read-only banner for Builders. Server enforces `require(Action.CODING_AGENT_WRITE)`;
 *  the banner is a UI affordance only. */
function BuilderReadOnlyBanner() {
  const slug = useCurrentOrgSlug();
  const membership = useMembership(slug);
  if (!membership || membership.role !== "builder") return null;
  return (
    <div
      className="rounded border border-info/40 bg-info/10 px-4 py-2 text-sm"
      data-testid="cc-builder-readonly"
    >
      <span className="font-semibold">View-only.</span> Builders see Coding Agent settings but can't
      change them. Ask an Admin in this org to update settings.
    </div>
  );
}

/** Warning block atop the Claude Code page when any enabled MCP provider for
 *  the current org has `last_refresh_status="failed"`. */
function BrokenIntegrationsNotice() {
  const { data: summary } = useBrokenSummary();
  const slug = useCurrentOrgSlug();
  const currentMembership = useMembership(slug);
  if (!summary || !currentMembership) return null;
  const orgEntry = summary.orgs.find((o) => o.org_id === currentMembership.org_id);
  if (!orgEntry || orgEntry.broken_integrations.length === 0) return null;
  const providers = orgEntry.broken_integrations.map((b) => b.provider).join(", ");
  return (
    <div
      className="rounded border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900"
      data-testid="cc-broken-integrations"
    >
      <span className="font-semibold">Reviews will receive `broken_creds` errors</span> from:{" "}
      {providers}. Reconnect in Org Settings → Integrations.
    </div>
  );
}
