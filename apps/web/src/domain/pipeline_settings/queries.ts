/**
 * Coding-agent picklist data for the stage editor's agent/model/effort
 * fields. Owned by `core/coding_agent` on the backend; model/effort defaults
 * are now embedded in each install row (`CodingAgentInstall.models` /
 * `.efforts`) so no separate defaults endpoint is needed.
 */

import { apiFetch } from "@core/api/public/client";
import { useSuspenseQuery } from "@tanstack/react-query";

export interface CodingAgentInstall {
  plugin_id: string;
  display_name: string;
  models: string[];
  efforts: string[];
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/** Coding agents installed on the current org — the stage editor's
 *  `coding_agent_plugin_id` picklist. Each row carries `display_name`,
 *  `models`, and `efforts` so the editor can populate dropdowns without
 *  a separate round-trip. */
export function useInstalledCodingAgents() {
  return useSuspenseQuery<CodingAgentInstall[]>({
    queryKey: ["coding-agents"],
    queryFn: () => apiFetch<CodingAgentInstall[]>("/api/coding-agents"),
    staleTime: 10_000,
  });
}
