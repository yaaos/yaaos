import { apiFetch } from "@core/api/public/client";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

export interface CodingAgentInstall {
  plugin_id: string;
  display_name: string;
  models: string[];
  efforts: string[];
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface AvailablePlugin {
  plugin_id: string;
  display_name: string;
}

export function useCodingAgents() {
  return useSuspenseQuery<CodingAgentInstall[]>({
    queryKey: ["coding-agents"],
    queryFn: () => apiFetch<CodingAgentInstall[]>("/api/coding-agents"),
    staleTime: 10_000,
  });
}

/** All registered coding-agent plugins regardless of install state. Used to
 *  populate the "Add coding agent" picker so admins see every available plugin. */
export function useAvailablePlugins() {
  return useSuspenseQuery<{ plugins: AvailablePlugin[] }>({
    queryKey: ["coding-agents", "available"],
    queryFn: () => apiFetch<{ plugins: AvailablePlugin[] }>("/api/coding-agents/available"),
    staleTime: 60_000,
  });
}

export function useInstallCodingAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { plugin_id: string; settings: Record<string, unknown> }) =>
      apiFetch<CodingAgentInstall>("/api/coding-agents", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["coding-agents"] }),
  });
}

export function useUninstallCodingAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (pluginId: string) =>
      apiFetch<{ removed: boolean }>(`/api/coding-agents/${encodeURIComponent(pluginId)}`, {
        method: "DELETE",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["coding-agents"] }),
  });
}
