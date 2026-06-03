import { apiFetch } from "@core/api";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

export interface AgentConfig {
  name: string;
  prompt: string;
  model: string;
  version: string;
  effort: string;
  updated_at: string;
  // — system-prompt override toggle per E2a.2. Both optional
  // so existing settings rows stay compatible.
  use_default_system_prompt?: boolean;
  system_prompt?: string | null;
}

export interface ClaudeCodeDefaults {
  orchestrator: AgentConfig;
  agents: AgentConfig[];
  models: string[];
  versions: string[];
  efforts: string[];
}

export function useClaudeCodeDefaults() {
  return useSuspenseQuery<ClaudeCodeDefaults>({
    queryKey: ["claude_code", "defaults"],
    queryFn: () => apiFetch<ClaudeCodeDefaults>("/api/claude_code/defaults"),
    staleTime: 60_000,
  });
}

export interface ByokProviderStatus {
  provider: string;
  status: "configured" | "not_set";
  last_validated_at: string | null;
  last_used_at: string | null;
  updated_at: string | null;
}

export function useByokAnthropicStatus() {
  return useSuspenseQuery<ByokProviderStatus | null>({
    queryKey: ["byok", "anthropic"],
    queryFn: async () => {
      const rows = await apiFetch<ByokProviderStatus[]>("/api/api-keys");
      return rows.find((r) => r.provider === "anthropic") ?? null;
    },
    staleTime: 10_000,
  });
}

export function useSetByokAnthropic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (value: string) =>
      apiFetch<{ status: string }>("/api/api-keys/anthropic", {
        method: "POST",
        body: JSON.stringify({ value }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["byok"] }),
  });
}

export function useValidateByokAnthropic() {
  return useMutation({
    mutationFn: () =>
      apiFetch<{ valid: boolean }>("/api/api-keys/anthropic/validate", { method: "POST" }),
  });
}

export function useClearByokAnthropic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<{ removed: boolean }>("/api/api-keys/anthropic", { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["byok"] }),
  });
}
