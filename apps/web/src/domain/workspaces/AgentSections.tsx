/**
 * AgentSections — groups agents into four sections by (state, lifecycle).
 *
 * Section membership rules:
 *   Active      — state != "offline" AND lifecycle == "active"
 *   Draining    — state != "offline" AND lifecycle == "draining"
 *   Unconfigured — state != "offline" AND lifecycle == "unconfigured"
 *   Inactive    — state == "offline" OR lifecycle == "shutdown"
 *
 * Sort within each section: last_heartbeat_at desc NULLS LAST.
 * Sections with zero agents are not rendered (hide-empty rule).
 *
 * Admin controls (checkboxes + bulk buttons) appear only in Active and
 * Draining sections when isAdmin=true. Selection state and mutation hooks
 * live in the parent (WorkspacesContent); props wire them in. Existing tests
 * that render AgentSections without admin props continue to work because
 * all admin props are optional with safe defaults.
 */

import type { AgentRow } from "@core/api/public/queries";
import { Button } from "@shared/components/ui/button";
import { Checkbox } from "@shared/components/ui/checkbox";
import { cn } from "@shared/utils/public/cn";
import { Activity, Cpu, HardDrive, Monitor } from "lucide-react";
import { useEffect, useState } from "react";

// ── Label maps ────────────────────────────────────────────────────────────────

const STATE_LABEL: Record<string, string> = {
  reachable: "Online",
  stale: "Stale",
  offline: "Offline",
};

const STATE_COLOR: Record<string, string> = {
  reachable: "text-success",
  stale: "text-warning",
  offline: "text-muted-foreground",
};

const LIFECYCLE_LABEL: Record<string, string> = {
  unconfigured: "Unconfigured",
  active: "Active",
  draining: "Draining",
  shutdown: "Shutdown",
};

// ── Partition + sort ──────────────────────────────────────────────────────────

interface Sections {
  active: AgentRow[];
  draining: AgentRow[];
  unconfigured: AgentRow[];
  inactive: AgentRow[];
}

function partitionAgents(agents: AgentRow[]): Sections {
  const active: AgentRow[] = [];
  const draining: AgentRow[] = [];
  const unconfigured: AgentRow[] = [];
  const inactive: AgentRow[] = [];

  for (const agent of agents) {
    if (agent.state === "offline" || agent.lifecycle === "shutdown") {
      inactive.push(agent);
    } else if (agent.lifecycle === "active") {
      active.push(agent);
    } else if (agent.lifecycle === "draining") {
      draining.push(agent);
    } else {
      // lifecycle == "unconfigured" (the remaining value in the enum)
      unconfigured.push(agent);
    }
  }

  return { active, draining, unconfigured, inactive };
}

/** Sort by last_heartbeat_at descending; null timestamps sort to the end. */
function sortByHeartbeat(agents: AgentRow[]): AgentRow[] {
  return [...agents].sort((a, b) => {
    if (!a.last_heartbeat_at && !b.last_heartbeat_at) return 0;
    if (!a.last_heartbeat_at) return 1;
    if (!b.last_heartbeat_at) return -1;
    return new Date(b.last_heartbeat_at).getTime() - new Date(a.last_heartbeat_at).getTime();
  });
}

// ── Relative-time label (auto-ticking every 5 s) ─────────────────────────────

function _relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 5) return "just now";
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function useRelativeTime(iso: string | null): string {
  const [label, setLabel] = useState(() => _relativeTime(iso));
  useEffect(() => {
    if (!iso) return;
    const id = setInterval(() => setLabel(_relativeTime(iso)), 5_000);
    return () => clearInterval(id);
  }, [iso]);
  return label;
}

// ── Agent card ────────────────────────────────────────────────────────────────

interface AgentCardProps {
  agent: AgentRow;
  isAdmin: boolean;
  hasAdminControls: boolean;
  selected: boolean;
  onToggle: (id: string, selected: boolean) => void;
}

function AgentCard({ agent, isAdmin, hasAdminControls, selected, onToggle }: AgentCardProps) {
  const lastSeen = useRelativeTime(agent.last_heartbeat_at);
  const memGb =
    agent.memory_bytes != null
      ? `${(agent.memory_bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
      : null;

  const stateLabel = STATE_LABEL[agent.state] ?? agent.state;
  const lifecycleLabel = LIFECYCLE_LABEL[agent.lifecycle] ?? agent.lifecycle;

  return (
    <div
      className="rounded-md border border-border bg-card p-4 flex flex-col gap-2 min-w-0"
      data-testid={`workspaces-agent-card-${agent.instance_id}`}
    >
      {/* Header: instance name + status pair */}
      <div className="flex items-start justify-between gap-2">
        {isAdmin && hasAdminControls && (
          <Checkbox
            className="mt-0.5 shrink-0"
            checked={selected}
            onCheckedChange={(checked) => onToggle(agent.id, !!checked)}
            data-testid={`workspaces-agent-card-${agent.instance_id}-select`}
            aria-label={`Select agent ${agent.instance_id}`}
          />
        )}
        <span className="text-sm font-medium truncate" title={agent.instance_id}>
          {agent.instance_id}
        </span>
        <span
          className={cn(
            "inline-flex items-center gap-1 text-xs font-medium shrink-0",
            STATE_COLOR[agent.state] ?? "text-muted-foreground",
          )}
          data-testid={`workspaces-agent-card-${agent.instance_id}-status`}
        >
          <Activity className="w-3 h-3" aria-hidden="true" />
          {stateLabel} / {lifecycleLabel}
        </span>
      </div>

      {/* Metadata row */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {agent.os && (
          <span className="flex items-center gap-1">
            <Monitor className="w-3 h-3" aria-hidden="true" />
            {agent.os}
          </span>
        )}
        {agent.cpu_count != null && (
          <span className="flex items-center gap-1">
            <Cpu className="w-3 h-3" aria-hidden="true" />
            {agent.cpu_count} CPUs
          </span>
        )}
        {memGb && (
          <span className="flex items-center gap-1">
            <HardDrive className="w-3 h-3" aria-hidden="true" />
            {memGb}
          </span>
        )}
        {agent.version && <span className="font-mono">{agent.version}</span>}
      </div>

      {/* Footer: workspace count + last seen */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {agent.claimed_workspace_count}{" "}
          {agent.claimed_workspace_count === 1 ? "workspace" : "workspaces"}
        </span>
        <span title={agent.last_heartbeat_at ?? "never connected"}>{lastSeen}</span>
      </div>
    </div>
  );
}

// ── Section component ─────────────────────────────────────────────────────────

type SectionName = "active" | "draining" | "unconfigured" | "inactive";

const SECTION_META: Record<
  SectionName,
  {
    title: string;
    testid: string;
    selectAllTestid?: string;
    actionTestid?: string;
    actionLabel?: string;
  }
> = {
  active: {
    title: "Active",
    testid: "workspaces-section-active",
    selectAllTestid: "workspaces-section-active-select-all",
    actionTestid: "workspaces-section-active-shutdown",
    actionLabel: "Shut down",
  },
  draining: {
    title: "Draining",
    testid: "workspaces-section-draining",
    selectAllTestid: "workspaces-section-draining-select-all",
    actionTestid: "workspaces-section-draining-cancel-shutdown",
    actionLabel: "Cancel shutdown",
  },
  unconfigured: { title: "Unconfigured", testid: "workspaces-section-unconfigured" },
  inactive: { title: "Inactive", testid: "workspaces-section-inactive" },
};

interface SectionProps {
  name: SectionName;
  agents: AgentRow[];
  isAdmin: boolean;
  selection: Set<string>;
  setSelection: (s: Set<string>) => void;
  onActionClick: () => void;
}

function Section({ name, agents, isAdmin, selection, setSelection, onActionClick }: SectionProps) {
  const meta = SECTION_META[name];
  const sorted = sortByHeartbeat(agents);
  const hasControls = name === "active" || name === "draining";

  const allSelected = sorted.length > 0 && sorted.every((a) => selection.has(a.id));
  const someSelected = sorted.some((a) => selection.has(a.id));

  function handleSelectAll() {
    if (allSelected) {
      setSelection(new Set());
    } else {
      setSelection(new Set(sorted.map((a) => a.id)));
    }
  }

  function handleToggle(id: string, checked: boolean) {
    const next = new Set(selection);
    if (checked) {
      next.add(id);
    } else {
      next.delete(id);
    }
    setSelection(next);
  }

  return (
    <section data-testid={meta.testid} className="mb-6">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          {isAdmin && hasControls && meta.selectAllTestid && (
            <Checkbox
              checked={someSelected ? (allSelected ? true : "indeterminate") : false}
              onCheckedChange={handleSelectAll}
              data-testid={meta.selectAllTestid}
              aria-label={`Select all ${meta.title} agents`}
            />
          )}
          <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
            {meta.title}
          </h2>
        </div>
        {isAdmin && hasControls && meta.actionTestid && (
          <Button
            variant="outline"
            size="sm"
            disabled={selection.size === 0}
            onClick={onActionClick}
            data-testid={meta.actionTestid}
          >
            {meta.actionLabel}
          </Button>
        )}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {sorted.map((agent) => (
          <AgentCard
            key={agent.id}
            agent={agent}
            isAdmin={isAdmin}
            hasAdminControls={hasControls}
            selected={selection.has(agent.id)}
            onToggle={handleToggle}
          />
        ))}
      </div>
    </section>
  );
}

// ── AgentSections (root export) ───────────────────────────────────────────────

export interface AgentSectionsProps {
  agents: AgentRow[];
  /** When true, checkboxes and bulk-action buttons appear on active/draining
   *  sections. False (default) hides all admin controls. */
  isAdmin?: boolean;
  activeSelection?: Set<string>;
  setActiveSelection?: (s: Set<string>) => void;
  drainingSelection?: Set<string>;
  setDrainingSelection?: (s: Set<string>) => void;
  /** Called when the admin clicks the "Shut down" button on the Active section. */
  onShutdownClick?: () => void;
  /** Called when the admin clicks the "Cancel shutdown" button on the Draining section. */
  onCancelShutdownClick?: () => void;
}

export function AgentSections({
  agents,
  isAdmin = false,
  activeSelection = new Set(),
  setActiveSelection = () => {},
  drainingSelection = new Set(),
  setDrainingSelection = () => {},
  onShutdownClick = () => {},
  onCancelShutdownClick = () => {},
}: AgentSectionsProps) {
  const { active, draining, unconfigured, inactive } = partitionAgents(agents);

  return (
    <>
      {active.length > 0 && (
        <Section
          name="active"
          agents={active}
          isAdmin={isAdmin}
          selection={activeSelection}
          setSelection={setActiveSelection}
          onActionClick={onShutdownClick}
        />
      )}
      {draining.length > 0 && (
        <Section
          name="draining"
          agents={draining}
          isAdmin={isAdmin}
          selection={drainingSelection}
          setSelection={setDrainingSelection}
          onActionClick={onCancelShutdownClick}
        />
      )}
      {unconfigured.length > 0 && (
        <Section
          name="unconfigured"
          agents={unconfigured}
          isAdmin={false}
          selection={new Set()}
          setSelection={() => {}}
          onActionClick={() => {}}
        />
      )}
      {inactive.length > 0 && (
        <Section
          name="inactive"
          agents={inactive}
          isAdmin={false}
          selection={new Set()}
          setSelection={() => {}}
          onActionClick={() => {}}
        />
      )}
    </>
  );
}
