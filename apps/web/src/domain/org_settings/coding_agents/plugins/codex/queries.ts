import { apiFetch } from "@core/api/public/client";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

export interface ApiKeyProviderStatus {
  provider: string;
  status: "configured" | "not_set";
  last_validated_at: string | null;
  last_used_at: string | null;
  updated_at: string | null;
}

export function useOpenAIKeyStatus() {
  return useSuspenseQuery<ApiKeyProviderStatus | null>({
    queryKey: ["api-keys", "openai"],
    queryFn: async () => {
      const rows = await apiFetch<ApiKeyProviderStatus[]>("/api/api-keys");
      return rows.find((r) => r.provider === "openai") ?? null;
    },
    staleTime: 10_000,
  });
}

export function useSetOpenAIKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (value: string) =>
      apiFetch<{ status: string }>("/api/api-keys/openai", {
        method: "POST",
        body: JSON.stringify({ value }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });
}

export function useValidateOpenAIKey() {
  return useMutation({
    mutationFn: () =>
      apiFetch<{ valid: boolean }>("/api/api-keys/openai/validate", {
        method: "POST",
      }),
  });
}

export function useClearOpenAIKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<{ removed: boolean }>("/api/api-keys/openai", {
        method: "DELETE",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });
}
