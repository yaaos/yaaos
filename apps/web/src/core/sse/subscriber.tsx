import { useCurrentOrgSlug } from "@core/api";
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
 * the browser `EventSource` API cannot send the `X-Org-Slug` header, so the
 * slug rides in the `?org=` query param (the backend accepts it for `/api/sse`
 * routes). When the active org changes the old stream is closed and a new one
 * opened; with no org in scope (`/login`, the org picker) there is no stream.
 *
 * Invalidations are coalesced by query-key prefix on a short trailing
 * debounce: a burst of N events that all target `["tickets"]` triggers a
 * single refetch rather than N.
 *
 * On every (re)connect (`onopen`) the list-level queries (`["tickets"]`,
 * `["reviewer", "metrics"]`) are invalidated to reconcile state: the stream
 * opens asynchronously and auto-reconnects after a drop, and any event
 * published while it was not OPEN is lost (Redis pub/sub has no replay).
 *
 * Translation table (`kind` → which query prefixes to invalidate):
 * - `ticket_status_changed` → ["tickets"], ["tickets", ticket_id],
 *   ["tickets", ticket_id, "audit"], ["reviewer", "metrics"]
 */

let _source: EventSource | null = null;
let _client: QueryClient | null = null;
let _slug: string | null = null;
let _connectedSlug: string | null = null;

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
    default:
      break;
  }
}

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
    return;
  }
  // Already streaming the right org.
  if (_source !== null && _connectedSlug === _slug) return;
  // First connect, or the org changed: drop the old stream, open the new one.
  if (_source !== null) {
    _source.close();
    _source = null;
  }
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
    _scheduleInvalidate(["tickets"]);
    _scheduleInvalidate(["reviewer", "metrics"]);
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
  // Native EventSource auto-reconnects with backoff. No close on transient
  // errors — only on tab teardown / org change, handled here.
  es.onerror = () => {};
}

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
