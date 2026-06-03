import { apiFetch } from "@core/api";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

export interface UserMembership {
  org_id: string;
  slug: string;
  display_name: string;
  role: "owner" | "admin" | "builder";
  handle: string;
}

export interface UserEmail {
  id: string;
  email: string;
  is_primary: boolean;
  verified: boolean;
}

export interface UserMe {
  user_id: string;
  display_name: string;
  github_username: string | null;
  emails: UserEmail[];
  memberships: UserMembership[];
}

/** Fetches `/api/user/me` — the richer per-user payload (per-org handles +
 * github_username). The skinnier `/api/auth/me` (user + memberships) stays
 * separate; chrome reads from `useCurrentUser`, this is the User
 * details/security page payload. */
export function useUserMe() {
  return useSuspenseQuery<UserMe>({
    queryKey: ["user", "me"],
    queryFn: () => apiFetch<UserMe>("/api/user/me"),
    staleTime: 30_000,
  });
}

export function useUpdateDisplayName() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (display_name: string) =>
      apiFetch<UserMe>("/api/user/me", {
        method: "PATCH",
        body: JSON.stringify({ display_name }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["user", "me"] }),
  });
}

export function useClearGithubUsername() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<UserMe>("/api/user/me", {
        method: "PATCH",
        body: JSON.stringify({ clear_github_username: true }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["user", "me"] }),
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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["user", "me"] }),
  });
}
