import { apiFetch } from "@core/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export interface IntegrationStatus {
  provider: string;
  /** "not_set" | "configured" | "broken" */
  status: string;
  enabled: boolean | null;
  upstream_identity: string | null;
  last_validated_at: string | null;
  last_refresh_failed_at: string | null;
  allowed_tools: string[];
}

export function useIntegrations() {
  return useQuery<IntegrationStatus[]>({
    queryKey: ["integrations"],
    queryFn: () => apiFetch<IntegrationStatus[]>("/api/integrations"),
    staleTime: 10_000,
  });
}

export interface PatchIntegrationRequest {
  allowed_tools?: string[];
  enabled?: boolean;
}

export function usePatchIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, body }: { provider: string; body: PatchIntegrationRequest }) =>
      apiFetch<IntegrationStatus>(`/api/integrations/${provider}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
}

export function useDeleteIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (provider: string) =>
      apiFetch<{ removed: boolean }>(`/api/integrations/${provider}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
}

export function useValidateIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (provider: string) =>
      apiFetch<{ valid: boolean }>(`/api/integrations/${provider}/validate`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
}
