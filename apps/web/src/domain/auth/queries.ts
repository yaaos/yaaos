import { apiFetch } from "@core/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export interface MembershipSummary {
  org_id: string;
  slug: string;
  display_name: string;
  role: "owner" | "admin" | "builder";
  handle: string;
}

export interface EmailSummary {
  email: string;
  is_primary: boolean;
  verified: boolean;
}

/**
 * Response of `GET /api/auth/me`.
 *
 * `memberships` are the authenticated user's *current* memberships — the
 * orgs they have access to in this session. Revoked memberships disappear
 * on the next call. The server has no opinion about which org is "current";
 * that's view state and lives in the URL.
 */
export interface CurrentUser {
  user: {
    id: string;
    display_name: string;
    primary_email: string | null;
    emails: EmailSummary[];
  };
  memberships: MembershipSummary[];
}

/** Fetches `/api/auth/me`. Returns `null` (not throws) when unauthenticated. */
export function useCurrentUser() {
  return useQuery<CurrentUser | null>({
    queryKey: ["auth", "me"],
    queryFn: async () => {
      try {
        return await apiFetch<CurrentUser>("/api/auth/me");
      } catch (err) {
        if ((err as Error)?.message?.startsWith("401")) return null;
        throw err;
      }
    },
    staleTime: 30_000,
  });
}

export function useProviders() {
  return useQuery<{ providers: string[] }>({
    queryKey: ["auth", "providers"],
    queryFn: () => apiFetch<{ providers: string[] }>("/api/auth/providers"),
    staleTime: 60_000,
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch("/api/auth/logout", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth"] }),
  });
}

export function useLogoutAll() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch("/api/auth/logout-all", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth"] }),
  });
}
