/**
 * Syncs authenticated identity into the OTel identity holder.
 *
 * Fetches the current user from /api/auth/me and calls setIdentity so that
 * subsequent spans carry yaaos.org_id / yaaos.user_id attributes. Clears
 * identity on 401. Runs in the root AppShell so it fires on every page load.
 *
 * Org slug is derived from the URL (no module-global cache, consistent with
 * how apiFetch reads org context). User ID comes from the /api/auth/me
 * response.
 */

import { useEffect } from "react";
import { getCurrentOrgSlug } from "../api/org-context";
import { setIdentity } from "./identity";

interface _AuthMeMinimal {
  user: { id: string };
}

/**
 * Call once in AppShell. Re-runs on every render (effect deps guarantee
 * identity is refreshed after org-slug navigation). The fetch is cheap —
 * TanStack Query already caches this endpoint at 30s.
 */
export function useOtelIdentitySync(): void {
  useEffect(() => {
    let cancelled = false;

    fetch("/api/auth/me", { credentials: "include" })
      .then(async (r) => {
        if (cancelled) return;
        if (!r.ok) {
          setIdentity(null);
          return;
        }
        const body = (await r.json()) as _AuthMeMinimal;
        if (cancelled) return;
        const orgSlug = getCurrentOrgSlug();
        setIdentity(orgSlug ? { orgId: orgSlug, userId: body.user.id } : null);
      })
      .catch(() => {
        if (!cancelled) setIdentity(null);
      });

    return () => {
      cancelled = true;
    };
  });
}
