import { apiFetch } from "@core/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export interface CodingAgentInstall {
  plugin_id: string;
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export function useCodingAgents() {
  return useQuery<CodingAgentInstall[]>({
    queryKey: ["coding-agents"],
    queryFn: () => apiFetch<CodingAgentInstall[]>("/api/coding-agents"),
    staleTime: 10_000,
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

export function useUpdateCodingAgentSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      pluginId,
      settings,
    }: {
      pluginId: string;
      settings: Record<string, unknown>;
    }) =>
      apiFetch<CodingAgentInstall>(`/api/coding-agents/${encodeURIComponent(pluginId)}`, {
        method: "PATCH",
        body: JSON.stringify({ settings }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["coding-agents"] }),
  });
}
