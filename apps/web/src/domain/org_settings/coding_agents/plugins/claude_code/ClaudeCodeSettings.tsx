import { useCurrentUser } from "@domain/auth";
import { ConfirmModal } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { useEffect, useState } from "react";
import {
  type CodingAgentInstall,
  useCodingAgents,
  useUninstallCodingAgent,
  useUpdateCodingAgentSettings,
} from "../../queries";
import {
  type AgentConfig,
  type ClaudeCodeDefaults,
  useByokAnthropicStatus,
  useClaudeCodeDefaults,
  useClearByokAnthropic,
  useSetByokAnthropic,
  useValidateByokAnthropic,
} from "./queries";

/**
 * Bespoke settings UI for the `claude_code` coding-agent plugin.
 *
 * Reads the org's stored settings via the generic /api/coding-agents list
 * endpoint + code defaults from the dedicated endpoint. Renders:
 *
 *  - One-paragraph architecture description (static).
 *  - Anthropic API key card (BYOK provider=anthropic — reveal/test/save/clear).
 *  - Orchestrator card (prompt, model, version, effort + reset-to-default + overridden badges).
 *  - Sub-agents list (1..8 — Add, Remove with last-protection, inline name uniqueness check).
 *  - Save button — replaces the entire settings JSONB in one PATCH.
 */
export function ClaudeCodeSettings({ pluginId }: { pluginId: string }) {
  const installs = useCodingAgents();
  const defaults = useClaudeCodeDefaults();
  const update = useUpdateCodingAgentSettings();

  const install = (installs.data ?? []).find((i) => i.plugin_id === pluginId);

  if (installs.isLoading || defaults.isLoading) {
    return <p className="text-muted-foreground p-4 text-sm">Loading…</p>;
  }
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
  if (!defaults.data) {
    return <p className="text-muted-foreground p-4 text-sm">Could not load defaults.</p>;
  }

  return <Editor install={install} defaults={defaults.data} update={update} />;
}

function Editor({
  install,
  defaults,
  update,
}: {
  install: CodingAgentInstall;
  defaults: ClaudeCodeDefaults;
  update: ReturnType<typeof useUpdateCodingAgentSettings>;
}) {
  const current = install.settings as { orchestrator?: AgentConfig; agents?: AgentConfig[] };
  const [orchestrator, setOrchestrator] = useState<AgentConfig>(
    current.orchestrator ?? defaults.orchestrator,
  );
  const [agents, setAgents] = useState<AgentConfig[]>(current.agents ?? defaults.agents);

  const duplicateNames = (() => {
    const names = agents.map((a) => a.name.trim());
    return names.filter((n, i) => n !== "" && names.indexOf(n) !== i);
  })();
  const hasDuplicateNames = duplicateNames.length > 0;
  const overCap = agents.length > 8;
  const underCap = agents.length < 1;
  const canSave = !hasDuplicateNames && !overCap && !underCap;

  const onSave = () => {
    if (!canSave) return;
    update.mutate({
      pluginId: install.plugin_id,
      settings: { orchestrator, agents },
    });
  };

  return (
    <div className="flex flex-col gap-4">
      <BrokenIntegrationsNotice />
      <BuilderReadOnlyBanner />
      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-[16px] font-semibold">Claude Code</h2>
        </header>
        <div className="px-4 py-4">
          <p className="text-muted-foreground text-sm">
            Claude Code runs as an orchestrator Claude session that delegates work to sub-agents via
            its Task tool. The orchestrator's prompt sets the overall task; each sub-agent's prompt
            sets a focused review pass run as its own Claude session.
          </p>
        </div>
      </section>

      <AnthropicKeyCard />

      <OrchestratorCard
        orchestrator={orchestrator}
        defaults={defaults}
        onChange={setOrchestrator}
      />

      <SubAgentsCard agents={agents} defaults={defaults} onChange={setAgents} />

      <div className="flex items-center gap-2">
        <Button data-testid="cc-save" disabled={!canSave || update.isPending} onClick={onSave}>
          {update.isPending ? "Saving…" : "Save"}
        </Button>
        {hasDuplicateNames && (
          <span className="text-xs text-destructive" data-testid="cc-duplicate-err">
            Duplicate sub-agent names: {duplicateNames.join(", ")}
          </span>
        )}
        {update.isError && (
          <span className="text-xs text-destructive" data-testid="cc-save-err">
            {(update.error as Error)?.message || "Save failed"}
          </span>
        )}
        {update.isSuccess && (
          <span className="text-xs text-emerald-600" data-testid="cc-save-ok">
            Saved.
          </span>
        )}
      </div>

      <DangerZone pluginId={install.plugin_id} />
    </div>
  );
}

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
        body="The plugin and its sub-agent configuration will be removed permanently. This cannot be undone."
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

/** M06 Phase 4: read-only banner for Builders. Per A1, Builders see Coding
 *  Agent settings (read access on the listing endpoint) but can't mutate
 *  org-wide config — the server-side `require(Action.CODING_AGENT_WRITE)`
 *  is the source of truth; the banner is UI affordance. */
function BuilderReadOnlyBanner() {
  const { data } = useCurrentUser();
  if (!data) return null;
  const currentOrg = data.orgs.find((o) => o.slug === data.current_org_slug);
  if (!currentOrg) return null;
  if (currentOrg.role !== "builder") return null;
  return (
    <div
      className="rounded border border-info/40 bg-info/10 px-4 py-2 text-sm"
      data-testid="cc-builder-readonly"
    >
      <span className="font-semibold">View-only.</span> Builders see Coding Agent settings but can't
      change them. Ask an Admin in this org to update model, sub-agents, or system prompts.
    </div>
  );
}

/** Warning block atop the Claude Code page when any enabled MCP provider for
 *  the current org has `last_refresh_status="failed"`. Reads from `/api/auth/me`'s
 *  `broken_integrations` since the page can't directly query integrations without
 *  the appropriate role context anyway. */
function BrokenIntegrationsNotice() {
  const { data } = useCurrentUser();
  if (!data) return null;
  const currentOrg = data.orgs.find((o) => o.slug === data.current_org_slug);
  if (!currentOrg || currentOrg.broken_integrations.length === 0) return null;
  const providers = currentOrg.broken_integrations.map((b) => b.provider).join(", ");
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

function AnthropicKeyCard() {
  const status = useByokAnthropicStatus();
  const setKey = useSetByokAnthropic();
  const validate = useValidateByokAnthropic();
  const clear = useClearByokAnthropic();
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);

  const configured = status.data?.status === "configured";

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
        <div className="flex items-center gap-2">
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            type={reveal ? "text" : "password"}
            placeholder={configured ? "•••• last 4 only — replace to update" : "sk-ant-..."}
            data-testid="cc-key-input"
            className="flex-1 rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
          />
          <Button data-testid="cc-key-reveal" onClick={() => setReveal((v) => !v)}>
            {reveal ? "Hide" : "Show"}
          </Button>
          <Button
            data-testid="cc-key-save"
            disabled={!value || setKey.isPending}
            onClick={() => setKey.mutate(value, { onSuccess: () => setValue("") })}
          >
            {setKey.isPending ? "Saving…" : "Save"}
          </Button>
          {configured && (
            <Button
              data-testid="cc-key-test"
              disabled={validate.isPending}
              onClick={() => validate.mutate()}
            >
              {validate.isPending ? "Testing…" : "Test"}
            </Button>
          )}
          {configured && (
            <Button
              data-testid="cc-key-clear"
              disabled={clear.isPending}
              onClick={() => clear.mutate()}
            >
              Clear
            </Button>
          )}
        </div>
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

function OrchestratorCard({
  orchestrator,
  defaults,
  onChange,
}: {
  orchestrator: AgentConfig;
  defaults: ClaudeCodeDefaults;
  onChange: (a: AgentConfig) => void;
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h3 className="text-[13.5px] font-semibold">Orchestrator</h3>
      </header>
      <div className="px-4 py-4">
        <AgentEditor
          agent={orchestrator}
          baseline={defaults.orchestrator}
          defaults={defaults}
          onChange={onChange}
          testIdPrefix="cc-orch"
        />
      </div>
    </section>
  );
}

function SubAgentsCard({
  agents,
  defaults,
  onChange,
}: {
  agents: AgentConfig[];
  defaults: ClaudeCodeDefaults;
  onChange: (a: AgentConfig[]) => void;
}) {
  const atCap = agents.length >= 8;
  const onLast = agents.length <= 1;

  const onAdd = () => {
    if (atCap) return;
    onChange([
      ...agents,
      {
        name: `sub-agent-${agents.length + 1}`,
        prompt: "",
        model: defaults.models[0] ?? "",
        version: defaults.versions[0] ?? "latest",
        effort: defaults.efforts[0] ?? "medium",
        updated_at: "",
      },
    ]);
  };

  const onRemove = (idx: number) => {
    if (onLast) return;
    onChange(agents.filter((_, i) => i !== idx));
  };

  const onAgentChange = (idx: number, next: AgentConfig) => {
    onChange(agents.map((a, i) => (i === idx ? next : a)));
  };

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <h3 className="text-[13.5px] font-semibold">Sub-agents ({agents.length}/8)</h3>
          <Button data-testid="cc-add-agent" disabled={atCap} onClick={onAdd}>
            Add sub-agent
          </Button>
        </div>
      </header>
      <div className="px-4 py-4">
        <div className="flex flex-col gap-3" data-testid="cc-agents-list">
          {agents.map((a, idx) => {
            const seededDefault = defaults.agents.find((d) => d.name === a.name);
            return (
              <div
                key={`${idx}-${a.name}`}
                className="rounded border border-border-soft p-3"
                data-testid={`cc-agent-${idx}`}
              >
                <AgentEditor
                  agent={a}
                  baseline={seededDefault}
                  defaults={defaults}
                  onChange={(next) => onAgentChange(idx, next)}
                  testIdPrefix={`cc-agent-${idx}`}
                  nameEditable
                />
                <div className="mt-2 flex justify-end">
                  <Button
                    data-testid={`cc-remove-agent-${idx}`}
                    disabled={onLast}
                    onClick={() => onRemove(idx)}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function AgentEditor({
  agent,
  baseline,
  defaults,
  onChange,
  testIdPrefix,
  nameEditable = false,
}: {
  agent: AgentConfig;
  baseline?: AgentConfig;
  defaults: ClaudeCodeDefaults;
  onChange: (next: AgentConfig) => void;
  testIdPrefix: string;
  nameEditable?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  // Auto-expand if prompt is short enough that a one-line preview is meaningless.
  useEffect(() => {
    if ((agent.prompt ?? "").length < 80) setExpanded(true);
  }, [agent.prompt]);

  const isOverridden = (field: keyof AgentConfig) =>
    baseline !== undefined && agent[field] !== baseline[field];
  const reset = (field: keyof AgentConfig) => {
    if (!baseline) return;
    onChange({ ...agent, [field]: baseline[field] });
  };

  return (
    <div className="flex flex-col gap-2 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground w-20 text-xs">Name</span>
        <input
          value={agent.name}
          disabled={!nameEditable}
          onChange={(e) => onChange({ ...agent, name: e.target.value })}
          data-testid={`${testIdPrefix}-name`}
          maxLength={64}
          className="flex-1 rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm disabled:opacity-60"
        />
        {isOverridden("name") && nameEditable && (
          <>
            <Badge variant="outline">overridden</Badge>
            <Button data-testid={`${testIdPrefix}-reset-name`} onClick={() => reset("name")}>
              Reset
            </Button>
          </>
        )}
      </div>
      <div className="flex items-start gap-2">
        <span className="text-muted-foreground w-20 pt-1.5 text-xs">Prompt</span>
        <div className="flex-1">
          {!expanded ? (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              data-testid={`${testIdPrefix}-prompt-expand`}
              className="text-muted-foreground w-full truncate rounded border border-border-soft bg-bg-2 px-2 py-1 text-left text-xs hover:bg-hover"
            >
              {(agent.prompt || "").slice(0, 120) || "(empty)"}
            </button>
          ) : (
            <textarea
              value={agent.prompt}
              onChange={(e) => onChange({ ...agent, prompt: e.target.value })}
              data-testid={`${testIdPrefix}-prompt`}
              rows={8}
              className="w-full rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
            />
          )}
        </div>
        {isOverridden("prompt") && (
          <div className="flex flex-col gap-1">
            <Badge variant="outline">overridden</Badge>
            <Button data-testid={`${testIdPrefix}-reset-prompt`} onClick={() => reset("prompt")}>
              Reset
            </Button>
          </div>
        )}
      </div>
      {/* M06 Phase 4: system-prompt override per E2a.2. Toggle disables the
          custom textarea; when off, the plugin uses its built-in system
          prompt for this agent. */}
      <div className="flex items-start gap-2">
        <span className="text-muted-foreground w-20 pt-1.5 text-xs">System prompt</span>
        <div className="flex-1 flex flex-col gap-2">
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={agent.use_default_system_prompt ?? true}
              onChange={(e) =>
                onChange({
                  ...agent,
                  use_default_system_prompt: e.target.checked,
                  // Clear stale override when toggling back to default.
                  system_prompt: e.target.checked ? null : (agent.system_prompt ?? ""),
                })
              }
              data-testid={`${testIdPrefix}-use-default-system-prompt`}
            />
            Use default system prompt
          </label>
          {!(agent.use_default_system_prompt ?? true) && (
            <textarea
              value={agent.system_prompt ?? ""}
              onChange={(e) => onChange({ ...agent, system_prompt: e.target.value })}
              data-testid={`${testIdPrefix}-system-prompt`}
              rows={4}
              placeholder="Override the built-in system prompt for this agent…"
              className="w-full rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
            />
          )}
        </div>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground w-20 text-xs">Model</span>
        <select
          value={agent.model}
          onChange={(e) => onChange({ ...agent, model: e.target.value })}
          data-testid={`${testIdPrefix}-model`}
          className="flex-1 rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
        >
          {defaults.models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <span className="text-muted-foreground text-xs">Version</span>
        <select
          value={agent.version}
          onChange={(e) => onChange({ ...agent, version: e.target.value })}
          data-testid={`${testIdPrefix}-version`}
          className="rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
        >
          {defaults.versions.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
        <span className="text-muted-foreground text-xs">Effort</span>
        <select
          value={agent.effort}
          onChange={(e) => onChange({ ...agent, effort: e.target.value })}
          data-testid={`${testIdPrefix}-effort`}
          className="rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
        >
          {defaults.efforts.map((eff) => (
            <option key={eff} value={eff}>
              {eff}
            </option>
          ))}
        </select>
      </div>
      {agent.updated_at && (
        <p className="text-muted-foreground text-[10.5px]">Updated {agent.updated_at}</p>
      )}
    </div>
  );
}
