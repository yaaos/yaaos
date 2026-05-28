import type { QueryClient } from "@tanstack/react-query";
import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect } from "react";
import type { ReviewJobActivityEvent } from "../api/client";
import { pushLiveActivity } from "./activity";
import type { ServerEvent } from "./types";

/** Mounted once at the app root. Translates server events into TanStack-Query
 * cache invalidations.
 *
 * The `EventSource` is owned by this module — NOT by the React effect. React
 * StrictMode double-invokes effects in dev; binding the connection lifetime
 * to component lifecycle leaves stale connections behind on every remount,
 * fanning out each server event N times and stampeding the dashboard.
 * Keeping one connection per tab and routing it to whichever QueryClient is
 * currently mounted fixes the storm without giving up the StrictMode lint.
 *
 * Invalidations are also coalesced by query-key prefix on a short
 * trailing debounce: a burst of N events that all target `["tickets"]`
 * triggers a single refetch rather than N.
 *
 * Translation table (`kind` → which query prefixes to invalidate):
 * - `ticket_status_changed` → ["tickets"], ["tickets", ticket_id],
 *   ["tickets", ticket_id, "audit"], ["reviewer", "metrics"]
 * - `review_job_status_changed` → ["reviewer", "jobs", ticket_id],
 *   ["tickets", ticket_id, "audit"], ["reviewer", "metrics"], ["tickets"]
 * - `review_job_step_progress` → ["reviewer", "jobs", ticket_id] (in-place;
 *   no metrics / list churn)
 * - `review_job_activity` → in-memory ring buffer via `pushLiveActivity`
 *   (no query invalidation — too high-frequency; `useLiveActivity` reads
 *   the tail)
 */

let _source: EventSource | null = null;
let _client: QueryClient | null = null;
let _started = false;

const _pendingKeys = new Map<string, readonly unknown[]>();
let _flushTimer: ReturnType<typeof setTimeout> | null = null;
const COALESCE_MS = 200;

function _scheduleInvalidate(queryKey: readonly unknown[]): void {
  if (_client === null) return;
  // Stringified key as a stable dedup id. JSON shape is deterministic for the
  // small set of keys we use; this avoids dependency on a deep-equal helper.
  const id = JSON.stringify(queryKey);
  _pendingKeys.set(id, queryKey);
  if (_flushTimer !== null) return;
  _flushTimer = setTimeout(() => {
    _flushTimer = null;
    const keys = Array.from(_pendingKeys.values());
    _pendingKeys.clear();
    const qc = _client;
    if (qc === null) return;
    for (const key of keys) {
      qc.invalidateQueries({ queryKey: key });
    }
  }, COALESCE_MS);
}

function _handleEvent(evt: ServerEvent): void {
  const tid = evt.ticket_id;
  switch (evt.kind) {
    case "ticket_status_changed":
      _scheduleInvalidate(["tickets"]);
      if (tid) {
        _scheduleInvalidate(["tickets", tid]);
        _scheduleInvalidate(["tickets", tid, "audit"]);
      }
      _scheduleInvalidate(["reviewer", "metrics"]);
      break;
    case "review_job_status_changed":
      if (tid) {
        _scheduleInvalidate(["reviewer", "jobs", tid]);
        _scheduleInvalidate(["tickets", tid, "audit"]);
      }
      _scheduleInvalidate(["reviewer", "metrics"]);
      _scheduleInvalidate(["tickets"]);
      break;
    case "review_job_step_progress":
      if (tid) _scheduleInvalidate(["reviewer", "jobs", tid]);
      break;
    case "review_job_activity": {
      const reviewJobId = typeof evt.review_job_id === "string" ? evt.review_job_id : null;
      const activityEvent = (evt.event ?? null) as ReviewJobActivityEvent | null;
      if (reviewJobId && activityEvent) pushLiveActivity(reviewJobId, activityEvent);
      break;
    }
    default:
      break;
  }
}

function _ensureConnection(): void {
  if (_started) return;
  if (typeof window === "undefined" || typeof EventSource === "undefined") return;
  _started = true;
  const es = new EventSource("/api/sse/general", { withCredentials: true });
  _source = es;
  es.onmessage = (msg) => {
    let evt: ServerEvent;
    try {
      evt = JSON.parse(msg.data);
    } catch {
      return;
    }
    _handleEvent(evt);
  };
  // Native EventSource auto-reconnects with backoff. No close on transient
  // errors — only on tab teardown, handled by the browser.
  es.onerror = () => {};
}

/** Test-only — used by vitest setup to start from a clean slate. Closes the
 * EventSource and clears pending invalidations. */
export function _resetSSESubscriberForTests(): void {
  if (_source !== null) _source.close();
  _source = null;
  _client = null;
  _started = false;
  _pendingKeys.clear();
  if (_flushTimer !== null) {
    clearTimeout(_flushTimer);
    _flushTimer = null;
  }
}

export function SSESubscriber({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  useEffect(() => {
    _client = qc;
    _ensureConnection();
    return () => {
      // Detach the QueryClient on unmount. The next mount (StrictMode or
      // normal) re-attaches without touching the EventSource. The
      // connection itself outlives the React tree.
      if (_client === qc) _client = null;
    };
  }, [qc]);

  return <>{children}</>;
}
