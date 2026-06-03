/**
 * Tests for the module-scope SSE store: subscribe/getSnapshot,
 * used as the backing for useSyncExternalStore consumers.
 *
 * Coverage:
 * - getSnapshot is referentially stable when state hasn't changed (no render loop)
 * - getSnapshot reflects status transitions: idle → connecting → connected → disconnected
 * - subscribe fires listeners when status changes
 * - subscribe fires listeners when a new event arrives
 * - debounce coalesces within 200ms (store snapshot reflects last event once timer fires)
 * - slug change reconnects and status transitions correctly
 * - _resetSSESubscriberForTests returns store to initial idle state
 */
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetSSESubscriberForTests,
  attachQueryClient,
  getSnapshot,
  setOrgSlug,
  subscribe,
} from "../subscriber";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  closed = false;
  url: string;
  withCredentials: boolean;

  constructor(url: string, init?: { withCredentials?: boolean }) {
    this.url = url;
    this.withCredentials = init?.withCredentials ?? false;
    FakeEventSource.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
  emit(payload: object): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
  open(): void {
    this.onopen?.();
  }
}

function live(): FakeEventSource[] {
  return FakeEventSource.instances.filter((es) => !es.closed);
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.instances = [];
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    FakeEventSource as unknown as typeof EventSource;
});

afterEach(() => {
  _resetSSESubscriberForTests();
  vi.useRealTimers();
});

describe("SSE store — getSnapshot", () => {
  it("returns idle status before any connection is established", () => {
    const snap = getSnapshot();
    expect(snap.status).toBe("idle");
    expect(snap.lastEvent).toBeNull();
  });

  it("is referentially stable between calls when state has not changed (no render loop)", () => {
    const snap1 = getSnapshot();
    const snap2 = getSnapshot();
    // Same object reference — useSyncExternalStore won't re-render on every call.
    expect(snap1).toBe(snap2);
  });

  it("returns a new reference (not the same object) after a status change", () => {
    const before = getSnapshot();
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const after = getSnapshot();
    // Status changed to connecting/connected, so snapshot must be a new object.
    expect(after).not.toBe(before);
  });

  it("reflects connected status after the stream opens", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected EventSource");
    es.open();
    expect(getSnapshot().status).toBe("connected");
  });

  it("reflects disconnected status after the stream errors", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected EventSource");
    es.onerror?.();
    expect(getSnapshot().status).toBe("disconnected");
  });

  it("reflects the lastEvent after an event arrives and timer fires", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected EventSource");

    const payload = {
      kind: "ticket_status_changed",
      ticket_id: "t1",
      source_module: "tickets",
      ts: "2026-01-01T00:00:00Z",
    };
    es.emit(payload);
    // Before timer fires the snapshot may not yet reflect the event (batching)
    // but after the 200ms debounce it must.
    vi.advanceTimersByTime(250);
    expect(getSnapshot().lastEvent).not.toBeNull();
    expect(getSnapshot().lastEvent?.kind).toBe("ticket_status_changed");
  });

  it("getSnapshot is referentially stable between identical consecutive events (no spurious re-renders)", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected EventSource");

    const payload = {
      kind: "ticket_status_changed",
      ticket_id: "t1",
      source_module: "tickets",
      ts: "2026-01-01T00:00:00Z",
    };
    es.emit(payload);
    vi.advanceTimersByTime(250);

    // Read the snapshot once (captures state after first event).
    const snap1 = getSnapshot();

    // Emit the exact same payload again; before the timer fires the snapshot
    // should not have changed (pending flush is still the same event data).
    es.emit(payload);
    const snap2 = getSnapshot();
    // During the debounce window, state hasn't committed — same reference.
    expect(snap2).toBe(snap1);
  });
});

describe("SSE store — subscribe", () => {
  it("fires the listener when the org slug is set (status changes)", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    const listener = vi.fn();
    const unsub = subscribe(listener);

    setOrgSlug("acme");
    expect(listener).toHaveBeenCalled();

    unsub();
  });

  it("fires the listener when an event arrives and the debounce timer fires", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected EventSource");

    const listener = vi.fn();
    const unsub = subscribe(listener);
    listener.mockClear();

    es.emit({ kind: "ticket_status_changed", ticket_id: "t1", source_module: "m", ts: "t" });
    vi.advanceTimersByTime(250);
    expect(listener).toHaveBeenCalled();

    unsub();
  });

  it("does not fire the listener after unsubscribe", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    const listener = vi.fn();
    const unsub = subscribe(listener);
    unsub();
    listener.mockClear();

    setOrgSlug("acme");
    expect(listener).not.toHaveBeenCalled();
  });

  it("coalesces burst: debounce ensures listener fires once per key-set flush", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected EventSource");

    const listener = vi.fn();
    const unsub = subscribe(listener);
    listener.mockClear();

    // Emit 5 events; within the 200ms window they should flush once.
    for (let i = 0; i < 5; i++) {
      es.emit({ kind: "ticket_status_changed", ticket_id: "t1", source_module: "m", ts: "t" });
    }
    expect(listener).not.toHaveBeenCalled();
    vi.advanceTimersByTime(250);
    // Listener fires once after the flush (store updated once).
    expect(listener).toHaveBeenCalledTimes(1);

    unsub();
  });
});

describe("SSE store — slug change reconnect", () => {
  it("status becomes idle/connecting when slug changes to a new org", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const first = live()[0];
    if (!first) throw new Error("expected EventSource");
    first.open();
    expect(getSnapshot().status).toBe("connected");

    // Change org: old stream closes, new stream opens.
    setOrgSlug("beta");
    // Status resets because the new stream hasn't opened yet.
    const snap = getSnapshot();
    expect(snap.status).not.toBe("connected");
  });

  it("status returns to idle when slug is cleared", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected EventSource");
    es.open();

    setOrgSlug(null);
    expect(getSnapshot().status).toBe("idle");
  });
});
