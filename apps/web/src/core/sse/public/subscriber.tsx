import { useCurrentOrgSlug } from "@core/api/public/org-context";
import type { QueryClient } from "@tanstack/react-query";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { ServerEvent } from "./types";

/** Drives the single browser-wide general-event `EventSource`, translating
 * server events into TanStack-Query cache invalidations. Wired up by
 * `useServerEvents()`, called once from the root `AppShell`.
 *
 * The connection is module-scoped — NOT owned by a React effect. React
 * StrictMode double-invokes effects in dev; binding the connection lifetime
 * to component lifecycle leaves stale connections behind on every remount,
 * fanning out each server event N times and stampeding the dashboard.
 * Keeping one connection per tab and routing it to whichever QueryClient is
 * currently mounted fixes the storm without giving up the StrictMode lint.
 *
 * The connection is keyed by org slug. `/api/sse/general` is org-scoped, but
 * the browser `EventSource` API cannot send the `X-Yaaos-Org-Slug` header, so the
 * slug rides in the `?org=` query param (the backend accepts it for `/api/sse`
 * routes). When the active org changes the old stream is closed and a new one
 * opened; with no org in scope (`/login`, the org picker) there is no stream.
 *
 * Invalidations are coalesced by query-key prefix on a short trailing
 * debounce: a burst of N events that all target `["tickets"]` triggers a
 * single refetch rather than N.
 *
 * On every (re)connect (`onopen`) the list-level queries (`["tickets"]`,
 * `["reviewer", "metrics"]`, `["agents"]`) are invalidated to reconcile state:
 * the stream opens asynchronously and auto-reconnects after a drop, and any
 * event published while it was not OPEN is lost (Redis pub/sub has no replay).
 *
 * Connection + last-event state is exposed via a module-scope store
 * (`subscribe`/`getSnapshot`) so React consumers can use
 * `useSyncExternalStore` for tear-free, concurrent-safe reads.
 *
 * Translation table (`kind` → which query prefixes to invalidate):
 * - `ticket_status_changed` → ["tickets"], ["tickets", ticket_id],
 *   ["tickets", ticket_id, "audit"], ["reviewer", "metrics"]
 * - `workflow_state_changed` → ["workflow", "runs", ticket_id],
 *   ["tickets", ticket_id] (status pill re-read)
 * - `review_requested` | `review_started` | `review_completed` |
 *   `review_failed` | `review_superseded` → ["tickets"], ["tickets", "dashboard"]
 * - `finding_raised` | `finding_re_observed` | `finding_anchor_updated` |
 *   `finding_state_changed` | `finding_acknowledged` |
 *   `finding_resolution_detected` | `finding_stale_detected` → ["tickets"], ["tickets", "dashboard"]
 * - `agent_liveness_changed` → ["agents"]
 */

// ---------------------------------------------------------------------------
// Connection status type
// ---------------------------------------------------------------------------

/** Connection state reported by the store snapshot. */
export type ConnectionStatus = "idle" | "connecting" | "connected" | "disconnected";

/** Immutable snapshot returned by `getSnapshot()`. */
export interface SSESnapshot {
  /** Current connection state. `idle` = no org in scope; `connecting` = stream
   * opened but `onopen` not yet fired; `connected` = OPEN; `disconnected` =
   * `onerror` fired (EventSource will auto-reconnect). */
  readonly status: ConnectionStatus;
  /** Last parsed `ServerEvent` received on the stream, or `null` before any
   * event arrives. Updated once per debounce flush (same cadence as
   * `invalidateQueries`). */
  readonly lastEvent: ServerEvent | null;
}

// ---------------------------------------------------------------------------
// Module-scope connection state (internal)
// ---------------------------------------------------------------------------

let _source: EventSource | null = null;
let _client: QueryClient | null = null;
let _slug: string | null = null;
let _connectedSlug: string | null = null;

const _pendingKeys = new Map<string, readonly unknown[]>();
let _flushTimer: ReturnType<typeof setTimeout> | null = null;
const COALESCE_MS = 200;

// ---------------------------------------------------------------------------
// Module-scope store (subscribe / getSnapshot)
// ---------------------------------------------------------------------------

// Snapshot is replaced (new object) whenever status or lastEvent changes.
// Callers that hold a reference across renders get the stale object; React
// detects the reference change via Object.is and re-renders only then.
let _snapshot: SSESnapshot = { status: "idle", lastEvent: null };
const _listeners = new Set<() => void>();

function _notifyListeners(): void {
  for (const l of _listeners) l();
}

function _setStatus(status: ConnectionStatus): void {
  if (_snapshot.status === status) return;
  _snapshot = { ..._snapshot, status };
  _notifyListeners();
}

function _setLastEvent(evt: ServerEvent): void {
  _snapshot = { ..._snapshot, lastEvent: evt };
  _notifyListeners();
}

/** Subscribe to store changes. Returns an unsubscribe function.
 * Used as the first argument to `useSyncExternalStore`. */
export function subscribe(listener: () => void): () => void {
  _listeners.add(listener);
  return () => {
    _listeners.delete(listener);
  };
}

/** Returns the current store snapshot. Referentially stable while state has
 * not changed — `useSyncExternalStore` uses `Object.is` to bail out of
 * re-renders. */
export function getSnapshot(): SSESnapshot {
  return _snapshot;
}

// ---------------------------------------------------------------------------
// Invalidation helpers
// ---------------------------------------------------------------------------

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
    case "workflow_state_changed":
      // Invalidate the workflow-run query for the specific ticket so the
      // Activity tab step tree and stage band refresh on every transition.
      // Also invalidate the ticket query so the status pill stays current.
      if (tid) {
        _scheduleInvalidate(["workflow", "runs", tid]);
        _scheduleInvalidate(["tickets", tid]);
        // Findings land as a workflow step completes; nothing else
        // invalidates this key, so refresh it on every transition.
        _scheduleInvalidate(["reviewer", "findings", tid]);
      }
      break;
    case "review_requested":
    case "review_started":
    case "review_completed":
    case "review_failed":
    case "review_superseded":
    case "finding_raised":
    case "finding_re_observed":
    case "finding_anchor_updated":
    case "finding_state_changed":
    case "finding_acknowledged":
    case "finding_resolution_detected":
    case "finding_stale_detected":
      _scheduleInvalidate(["tickets"]);
      _scheduleInvalidate(["tickets", "dashboard"]);
      break;
    case "agent_liveness_changed":
      _scheduleInvalidate(["agents"]);
      break;
    default:
      break;
  }
  // Store the last event; update fires after the debounce so the snapshot
  // update is co-located with the query invalidation flush.
  // We schedule the store notification on the same timer cadence by updating
  // the snapshot inside a separate trailing call that piggybacks the flush.
  _schedulePendingEvent(evt);
}

// Tracks the most-recent event waiting to commit to the snapshot. Updated
// synchronously on each parsed event; committed (snapshot replaced + listeners
// notified) when the debounce flush runs.
let _pendingEvent: ServerEvent | null = null;
let _eventFlushTimer: ReturnType<typeof setTimeout> | null = null;

function _schedulePendingEvent(evt: ServerEvent): void {
  _pendingEvent = evt;
  if (_eventFlushTimer !== null) return;
  _eventFlushTimer = setTimeout(() => {
    _eventFlushTimer = null;
    const e = _pendingEvent;
    _pendingEvent = null;
    if (e !== null) _setLastEvent(e);
  }, COALESCE_MS);
}

// ---------------------------------------------------------------------------
// Connection lifecycle
// ---------------------------------------------------------------------------

/** Open/close the stream to match (`_client`, `_slug`). Idempotent: a no-op
 * when already connected to the right org, so StrictMode double-invokes and
 * repeated slug reports don't churn the connection. */
function _syncConnection(): void {
  if (_client === null) return;
  if (typeof window === "undefined" || typeof EventSource === "undefined") return;
  // No org in scope → ensure disconnected.
  if (_slug === null) {
    if (_source !== null) {
      _source.close();
      _source = null;
      _connectedSlug = null;
    }
    _setStatus("idle");
    return;
  }
  // Already streaming the right org.
  if (_source !== null && _connectedSlug === _slug) return;
  // First connect, or the org changed: drop the old stream, open the new one.
  if (_source !== null) {
    _source.close();
    _source = null;
  }
  _setStatus("connecting");
  const es = new EventSource(`/api/sse/general?org=${encodeURIComponent(_slug)}`, {
    withCredentials: true,
  });
  _source = es;
  _connectedSlug = _slug;
  // Reconcile on every (re)connect. The stream opens asynchronously and
  // auto-reconnects after a drop; any event published while it was not OPEN
  // is gone (Redis pub/sub has no replay). Refetching the list-level queries
  // on open recovers a ticket created in that window without a manual reload.
  es.onopen = () => {
    _setStatus("connected");
    _scheduleInvalidate(["tickets"]);
    _scheduleInvalidate(["reviewer", "metrics"]);
    _scheduleInvalidate(["agents"]);
  };
  es.onmessage = (msg) => {
    let evt: ServerEvent;
    try {
      evt = JSON.parse(msg.data);
    } catch {
      return;
    }
    _handleEvent(evt);
  };
  // Native EventSource auto-reconnects with backoff. Signal disconnected so
  // consumers can render "reconnecting…" but do not close — the browser
  // handles reconnection.
  es.onerror = () => {
    _setStatus("disconnected");
  };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/** Route invalidations into `qc` and (re)connect if an org is in scope. */
export function attachQueryClient(qc: QueryClient): void {
  _client = qc;
  _syncConnection();
}

/** Detach `qc` without touching the connection — it outlives the React tree
 * so StrictMode/route remounts don't churn it. */
export function detachQueryClient(qc: QueryClient): void {
  if (_client === qc) _client = null;
}

/** Set the active org slug; opens/closes/re-targets the stream as needed. */
export function setOrgSlug(slug: string | null): void {
  _slug = slug;
  _syncConnection();
}

/** Test-only — used by vitest setup to start from a clean slate. Closes the
 * EventSource and clears pending invalidations + slug state. */
export function _resetSSESubscriberForTests(): void {
  if (_source !== null) _source.close();
  _source = null;
  _client = null;
  _slug = null;
  _connectedSlug = null;
  _pendingKeys.clear();
  if (_flushTimer !== null) {
    clearTimeout(_flushTimer);
    _flushTimer = null;
  }
  _pendingEvent = null;
  if (_eventFlushTimer !== null) {
    clearTimeout(_eventFlushTimer);
    _eventFlushTimer = null;
  }
  // Reset snapshot and listeners.
  _snapshot = { status: "idle", lastEvent: null };
  _listeners.clear();
}

/** Mounted once via the root `AppShell`. Attaches the current QueryClient and
 * keeps the stream pointed at the active org (from the URL). */
export function useServerEvents(): void {
  const qc = useQueryClient();
  const slug = useCurrentOrgSlug();
  useEffect(() => {
    attachQueryClient(qc);
    return () => detachQueryClient(qc);
  }, [qc]);
  useEffect(() => {
    setOrgSlug(slug);
  }, [slug]);
}
