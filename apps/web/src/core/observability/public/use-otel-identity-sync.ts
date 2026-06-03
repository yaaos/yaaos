/**
 * Passive reader of the shared `useCurrentUser` cache; never issues its own fetch.
 *
 * Subscribes to the `["auth","me"]` cache entry via `useQuery({ enabled: false })` —
 * safe on pre-auth pages such as `/login` where the cache is empty and a fetch would
 * loop (the queryFn routes through `apiFetch`, whose 401 handler hard-redirects to
 * `/login`). When the cache is empty, identity stays null. When `UserCard` mounts on
 * authenticated pages and `useCurrentUser` populates the cache, this hook re-renders
 * and stamps the identity. Identity is URL-scoped via `orgSlug`; null on `/login`
 * (no org slug in the URL) even if the cache holds a user.
 */

import { getCurrentOrgSlug } from "@core/api/public/org-context";
import { currentUserQueryOptions } from "@core/api/public/queries";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { setIdentity } from "../identity";

/**
 * Call once in AppShell. Passively reads the `["auth","me"]` cache populated by
 * `useCurrentUser` (via `UserCard`). Never fetches. Identity is null on pre-auth
 * pages (empty cache) and on any page without an org slug in the URL.
 */
export function useOtelIdentitySync(): void {
  const orgSlug = getCurrentOrgSlug();
  const { data } = useQuery({ ...currentUserQueryOptions, enabled: false });

  useEffect(() => {
    const userId = data?.user.id ?? null;
    setIdentity(orgSlug && userId ? { orgId: orgSlug, userId } : null);
  }, [orgSlug, data]);
}
