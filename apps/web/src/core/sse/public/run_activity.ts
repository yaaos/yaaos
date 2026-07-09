/** Per-run live activity-tail SSE hook.
 *
 * Opens an `EventSource` to `/api/sse/workspace_activity/{runId}` when
 * `runId` is non-null; closes on unmount or when `runId` changes. The
 * connection drives the backend's `SubscriberRegistry.track/untrack` via the
 * lifecycle hooks registered at `core/agent_gateway` import time.
 *
 * Normalizes each server frame to `ReviewJobActivityEvent` — the same shape
 * the Runs-tab `ActivityEventRow` already renders, so no per-consumer
 * adaptation is needed. Unknown or missing fields fall back to safe defaults.
 *
 * Keeps the last 500 events; a runaway stage must not grow an unbounded list.
 * On unmount (or `runId` change), the EventSource is closed and the event
 * list is reset so the next mount starts clean.
 *
 * Returns `connected: true` once `onopen` has fired — callers that need to
 * guarantee no events are missed (e.g. e2e tests publishing a synthetic frame)
 * should wait for this flag before publishing.
 */

import type { ReviewJobActivityEvent } from "@core/api/public/client";
import { getCurrentOrgSlug } from "@core/api/public/org-context";
import { useEffect, useState } from "react";

const MAX_EVENTS = 500;

export function useRunActivityTail(runId: string | null): {
  events: ReviewJobActivityEvent[];
  lastEvent: ReviewJobActivityEvent | null;
  connected: boolean;
} {
  const [events, setEvents] = useState<ReviewJobActivityEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!runId) return;
    // `/api/sse` is org-scoped, but EventSource cannot send X-Yaaos-Org-Slug —
    // the slug rides in `?org=` instead (backend accepts it for SSE routes).
    const slug = getCurrentOrgSlug();
    const url = slug
      ? `/api/sse/workspace_activity/${runId}?org=${encodeURIComponent(slug)}`
      : `/api/sse/workspace_activity/${runId}`;
    const es = new EventSource(url, { withCredentials: true });
    es.onopen = () => {
      // Connection confirmed — the backend's Redis subscription is established
      // (subscription-before-prelude ordering in the server guarantees this)
      // and any publish after this point is guaranteed to arrive.
      setConnected(true);
    };
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as Partial<ReviewJobActivityEvent>;
        const normalized: ReviewJobActivityEvent = {
          kind: typeof data.kind === "string" ? data.kind : "unknown",
          ts: typeof data.ts === "string" ? data.ts : new Date().toISOString(),
          message: typeof data.message === "string" ? data.message : "",
          detail:
            typeof data.detail === "object" && data.detail !== null
              ? (data.detail as Record<string, unknown>)
              : null,
        };
        setEvents((prev) => {
          const next = [...prev, normalized];
          return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
        });
      } catch {
        // Ignore malformed frames — server logs them.
      }
    };
    es.onerror = () => {
      // Standard EventSource auto-reconnect handles transient drops; no
      // action needed here. On hard failure the browser stops retrying.
    };
    return () => {
      es.close();
      setEvents([]);
      setConnected(false);
    };
  }, [runId]);

  const lastEvent = events.length > 0 ? (events[events.length - 1] ?? null) : null;
  return { events, lastEvent, connected };
}
