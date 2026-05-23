import { apiFetch } from "@core/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export interface AccountOrg {
  org_id: string;
  slug: string;
  display_name: string;
  role: "owner" | "admin" | "builder";
  handle: string;
}

export interface AccountEmail {
  id: string;
  email: string;
  is_primary: boolean;
  verified: boolean;
}

export interface AccountMe {
  user_id: string;
  display_name: string;
  github_username: string | null;
  emails: AccountEmail[];
  orgs: AccountOrg[];
}

/** Fetches `/api/account/me`. The /api/auth/me endpoint is the M02 surface;
 * this richer payload (per-org handles + github_username) lands with M03. */
export function useAccountMe() {
  return useQuery<AccountMe>({
    queryKey: ["account", "me"],
    queryFn: () => apiFetch<AccountMe>("/api/account/me"),
    staleTime: 30_000,
  });
}

export function useUpdateDisplayName() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (display_name: string) =>
      apiFetch<AccountMe>("/api/account/me", {
        method: "PATCH",
        body: JSON.stringify({ display_name }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["account", "me"] }),
  });
}

export function useClearGithubUsername() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<AccountMe>("/api/account/me", {
        method: "PATCH",
        body: JSON.stringify({ clear_github_username: true }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["account", "me"] }),
  });
}

export function useUpdateOrgHandle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ orgId, handle }: { orgId: string; handle: string }) =>
      apiFetch<unknown>(`/api/memberships/me/${orgId}`, {
        method: "PATCH",
        body: JSON.stringify({ handle }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["account", "me"] }),
  });
}
