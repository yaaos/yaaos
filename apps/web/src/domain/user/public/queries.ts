import { apiFetch } from "@core/api/public/client";
import { useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

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

// ---------------------------------------------------------------------------
// OAuth user connections
// ---------------------------------------------------------------------------

export interface OAuthConnectionView {
  provider_id: string;
  display_name: string;
  connect_hint: string;
  status: "not_connected" | "connected" | "needs_reauth";
  external_account_id: string | null;
  connected_at: string | null;
  needs_reauth_reason: string | null;
}

export interface DeviceAuthStart {
  verification_url: string;
  user_code: string;
  expires_at: string;
  poll_interval_seconds: number;
}

export interface DeviceAuthPoll {
  status: string;
  poll_interval_seconds: number | null;
}

/** `["user-oauth-connections"]` — list of all registered OAuth apps with the
 * caller's connection status. `useSuspenseQuery` so the section skeleton
 * renders while loading. */
export function useOAuthConnections() {
  return useSuspenseQuery<OAuthConnectionView[]>({
    queryKey: ["user-oauth-connections"],
    queryFn: () =>
      apiFetch<{ connections: OAuthConnectionView[] }>("/api/user/oauth/connections").then(
        (d) => d.connections,
      ),
    staleTime: 30_000,
  });
}

/** Start device-auth handshake for `providerId`. */
export function useStartDeviceAuth(providerId: string) {
  return useMutation({
    mutationFn: () =>
      apiFetch<DeviceAuthStart>(`/api/user/oauth/${providerId}/device-auth/start`, {
        method: "POST",
      }),
  });
}

/** Poll device-auth status for `providerId`.
 *
 * `enabled` controls whether the query fires. `refetchInterval` drives the
 * polling cadence: stop when non-pending, otherwise use the last-returned
 * `poll_interval_seconds` (or 5 s as the default). */
export function usePollDeviceAuth(providerId: string, enabled: boolean) {
  return useQuery<DeviceAuthPoll>({
    queryKey: ["user-oauth-device-poll", providerId],
    queryFn: () =>
      apiFetch<DeviceAuthPoll>(`/api/user/oauth/${providerId}/device-auth/poll`, {
        method: "POST",
      }),
    enabled,
    staleTime: 0,
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status || status !== "pending") return false;
      return (query.state.data?.poll_interval_seconds ?? 5) * 1000;
    },
  });
}

/** Disconnect the OAuth connection for `providerId`. */
export function useDisconnectOAuth(providerId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<{ removed: boolean }>(`/api/user/oauth/${providerId}/connection`, {
        method: "DELETE",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["user-oauth-connections"] }),
  });
}
