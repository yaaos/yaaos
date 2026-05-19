import { apiFetch } from "@core/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export interface MembershipSummary {
  slug: string;
  display_name: string;
  role: "owner" | "admin" | "member";
  handle: string;
}

export interface EmailSummary {
  email: string;
  is_primary: boolean;
  verified: boolean;
}

export interface CurrentUser {
  user: {
    id: string;
    display_name: string;
    primary_email: string | null;
    emails: EmailSummary[];
  };
  orgs: MembershipSummary[];
  current_org_slug: string | null;
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
