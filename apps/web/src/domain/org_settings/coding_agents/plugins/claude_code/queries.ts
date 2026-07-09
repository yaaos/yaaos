import { apiFetch } from "@core/api/public/client";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

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
