import { apiFetch } from "@core/api";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

export interface ByokProviderStatus {
  provider: string;
  status: "configured" | "not_set";
  last_validated_at: string | null;
  last_used_at: string | null;
  updated_at: string | null;
}

export function useByokProviders() {
  return useSuspenseQuery<ByokProviderStatus[]>({
    queryKey: ["byok"],
    queryFn: () => apiFetch<ByokProviderStatus[]>("/api/api-keys"),
    staleTime: 10_000,
  });
}

export function useSetByok() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, value }: { provider: string; value: string }) =>
      apiFetch<{ status: string }>(`/api/api-keys/${encodeURIComponent(provider)}`, {
        method: "POST",
        body: JSON.stringify({ value }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["byok"] }),
  });
}

export function useValidateByok() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (provider: string) =>
      apiFetch<{ valid: boolean }>(`/api/api-keys/${encodeURIComponent(provider)}/validate`, {
        method: "POST",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["byok"] }),
  });
}

export function useClearByok() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (provider: string) =>
      apiFetch<{ removed: boolean }>(`/api/api-keys/${encodeURIComponent(provider)}`, {
        method: "DELETE",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["byok"] }),
  });
}
