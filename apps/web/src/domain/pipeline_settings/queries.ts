/**
 * Coding-agent picklist data for the stage editor's agent/model/effort
 * fields. Both endpoints are owned by other modules on the backend
 * (`domain/orgs` installs, `plugins/claude_code` defaults) but this module
 * hits them directly — same pattern as `domain/org_settings/coding_agents`,
 * just a second, independent consumer of the same REST surface (no
 * cross-domain import; each domain module owns its own thin query hook).
 */

import { apiFetch } from "@core/api/public/client";
import { useSuspenseQuery } from "@tanstack/react-query";

export interface CodingAgentInstall {
  plugin_id: string;
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/** Coding agents installed on the current org — the stage editor's
 *  `coding_agent_plugin_id` picklist. */
export function useInstalledCodingAgents() {
  return useSuspenseQuery<CodingAgentInstall[]>({
    queryKey: ["coding-agents"],
    queryFn: () => apiFetch<CodingAgentInstall[]>("/api/coding-agents"),
    staleTime: 10_000,
  });
}

export interface ClaudeCodeDefaults {
  models: string[];
  efforts: string[];
}

/** `claude_code`'s advertised model/effort dropdown values. `claude_code` is
 *  the only registered coding-agent plugin today — the stage editor's
 *  model/effort Selects read this regardless of which plugin the admin
 *  picked, since there's nothing else to read yet. */
export function useClaudeCodeDefaults() {
  return useSuspenseQuery<ClaudeCodeDefaults>({
    queryKey: ["claude-code", "defaults"],
    queryFn: () => apiFetch<ClaudeCodeDefaults>("/api/claude_code/defaults"),
    staleTime: 60_000,
  });
}
