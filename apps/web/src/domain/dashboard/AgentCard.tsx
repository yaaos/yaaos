/**
 * AgentCard — richer card for a single workspace agent displayed on the
 * dashboard's "Workspace agents" row. Shows liveness state, last seen (relative
 * and auto-ticking), and resource metadata.
 *
 * Liveness colors:
 *   reachable → success (green)
 *   stale     → warning (amber)
 *   offline   → muted  (gray)
 */

import type { AgentRow } from "@core/api";
import { cn } from "@shared/utils";
import { Link } from "@tanstack/react-router";
import { Activity, Cpu, HardDrive, Monitor } from "lucide-react";
import { useEffect, useState } from "react";

// ── Relative-time formatting ───────────────────────────────────────────────

function _relativeTime(iso: string | null): string {
  if (!iso) return "never";
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

// ── State badge ───────────────────────────────────────────────────────────

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

function StateBadge({ state }: { state: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 text-xs font-medium",
        STATE_COLOR[state] ?? "text-muted-foreground",
      )}
      data-testid={`agent-state-${state}`}
    >
      <Activity className="w-3 h-3" />
      {STATE_LABEL[state] ?? state}
    </span>
  );
}

// ── AgentCard ─────────────────────────────────────────────────────────────

interface AgentCardProps {
  agent: AgentRow;
}

export function AgentCard({ agent }: AgentCardProps) {
  const lastSeen = useRelativeTime(agent.last_heartbeat_at);
  const memGb =
    agent.memory_bytes != null
      ? `${(agent.memory_bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
      : null;

  return (
    <div
      className="rounded-md border border-border bg-card p-4 flex flex-col gap-2 min-w-0"
      data-testid={`agent-card-instance-${agent.instance_id}`}
    >
      {/* Header row: instance name + state badge */}
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium truncate" title={agent.instance_id}>
          {agent.instance_id}
        </span>
        <StateBadge state={agent.state} />
      </div>

      {/* Metadata row */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {agent.os && (
          <span className="flex items-center gap-1">
            <Monitor className="w-3 h-3" />
            {agent.os}
          </span>
        )}
        {agent.cpu_count != null && (
          <span className="flex items-center gap-1">
            <Cpu className="w-3 h-3" />
            {agent.cpu_count} CPUs
          </span>
        )}
        {memGb && (
          <span className="flex items-center gap-1">
            <HardDrive className="w-3 h-3" />
            {memGb}
          </span>
        )}
      </div>

      {/* Footer: workspaces + last seen */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {agent.claimed_workspace_count} workspace{agent.claimed_workspace_count !== 1 ? "s" : ""}
        </span>
        <span title={agent.last_heartbeat_at ?? ""}>{lastSeen}</span>
      </div>
    </div>
  );
}

// ── AgentCardEmpty ─────────────────────────────────────────────────────────

/** Shown when no agents are connected. */
export function AgentCardEmpty() {
  return (
    <div className="rounded-md border border-border bg-card p-4 flex flex-col gap-1 text-sm text-muted-foreground">
      <span className="font-medium text-foreground">No WorkspaceAgents connected.</span>
      <span>
        Set your IAM role in{" "}
        <Link
          to="/orgs/$slug/settings/workspaces"
          params={(prev) => ({ slug: (prev as { slug?: string }).slug ?? "" })}
          className="underline underline-offset-2 hover:text-foreground transition-colors"
          data-testid="agent-card-empty-settings-link"
        >
          Workspaces settings
        </Link>{" "}
        to get started.
      </span>
    </div>
  );
}
