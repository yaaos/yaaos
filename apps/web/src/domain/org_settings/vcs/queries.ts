import { apiFetch } from "@core/api/public/client";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

export interface VcsStateResponse {
  plugin_id: string | null;
  settings: Record<string, unknown>;
}

export function useVcsState() {
  return useSuspenseQuery<VcsStateResponse>({
    queryKey: ["vcs", "state"],
    queryFn: () => apiFetch<VcsStateResponse>("/api/vcs"),
    staleTime: 10_000,
  });
}

export function useClearVcs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<VcsStateResponse>("/api/vcs", { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["vcs"] }),
  });
}

export interface StartGithubInstallResponse {
  redirect_url: string;
}

/** Owner-only — POSTs to start the GitHub App install handshake and returns
 *  the state-signed github.com URL the SPA should navigate to. The auth chain
 *  needs `X-Yaaos-Org-Slug` + `X-CSRF-Token` on the request, which `apiFetch` sets;
 *  a top-level browser nav to the same route would 401 (no headers). */
export function useStartGithubInstall() {
  return useMutation({
    mutationFn: () =>
      apiFetch<StartGithubInstallResponse>("/api/github/install/start", {
        method: "POST",
      }),
  });
}
