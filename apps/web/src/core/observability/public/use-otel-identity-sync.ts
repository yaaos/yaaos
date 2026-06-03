/**
 * Syncs authenticated identity into the OTel identity holder.
 *
 * This is a passive telemetry probe: it fetches `/api/auth/me` to attribute
 * spans with org slug + user id, and must never drive navigation. It runs in
 * the root AppShell, so it fires on pre-auth pages (e.g. `/login`) too — a 401
 * there is expected. It therefore does NOT route through `apiFetch`, whose
 * centralized 401 handler hard-redirects to `/login`; doing so would create an
 * infinite reload loop on the login page. Real auth failures still redirect
 * via each page's actual data queries (those go through `apiFetch`).
 *
 * On 200, calls `setIdentity` with org slug + user id. On 401, clears identity
 * silently (no redirect). On any other error (5xx, network failure), leaves
 * identity intact and records the error via `recordException`. Re-runs when the
 * org slug derived from the URL changes.
 *
 * The body is typed as `CurrentUser` (the generated `/api/auth/me` type is
 * `unknown`, so this matches what `apiFetch<CurrentUser>` would itself cast to).
 */

import { getCurrentOrgSlug } from "@core/api/public/org-context";
import type { CurrentUser } from "@core/api/public/queries";
import { useEffect } from "react";
import { setIdentity } from "../identity";
import { recordException } from "./sdk";

/**
 * Call once in AppShell. Re-runs when the org slug in the URL changes.
 * Clears identity silently on 401 (no navigation). Leaves identity intact and
 * records the error on transient failures (5xx, network errors).
 */
export function useOtelIdentitySync(): void {
  const orgSlug = getCurrentOrgSlug();

  useEffect(() => {
    let cancelled = false;

    fetch("/api/auth/me", { credentials: "include" })
      .then(async (r) => {
        if (cancelled) return;
        if (r.status === 401) {
          // Pre-auth or expired session. Clear identity; do NOT redirect —
          // this probe is not the owner of the auth-failure flow.
          setIdentity(null);
          return;
        }
        if (!r.ok) {
          // Transient error (5xx): leave identity intact. The user is still
          // authenticated; a momentary outage shouldn't blank their org
          // context on in-flight spans.
          recordException(new Error(`${r.status} /api/auth/me`));
          return;
        }
        const body = (await r.json()) as CurrentUser;
        if (cancelled) return;
        setIdentity(orgSlug ? { orgId: orgSlug, userId: body.user.id } : null);
      })
      .catch((err: unknown) => {
        // Network failure: leave identity intact, record the error.
        if (!cancelled) recordException(err);
      });

    return () => {
      cancelled = true;
    };
  }, [orgSlug]);
}
