import { apiFetch } from "@core/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export interface VcsStateResponse {
  plugin_id: string | null;
  settings: Record<string, unknown>;
}

export interface SetVcsRequest {
  plugin_id: string;
  settings: Record<string, unknown>;
}

export interface SetVcsResponse {
  state: VcsStateResponse | null;
  install_url: string | null;
}

export function useVcsState() {
  return useQuery<VcsStateResponse>({
    queryKey: ["vcs", "state"],
    queryFn: () => apiFetch<VcsStateResponse>("/api/vcs"),
    staleTime: 10_000,
  });
}

export function useSetVcs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SetVcsRequest) =>
      apiFetch<SetVcsResponse>("/api/vcs", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["vcs"] }),
  });
}

export function useClearVcs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<VcsStateResponse>("/api/vcs", { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["vcs"] }),
  });
}
