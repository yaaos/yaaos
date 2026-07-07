import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _resetSSESubscriberForTests, attachQueryClient, setOrgSlug } from "../public/subscriber";

/** Captured per-test so we can verify dispatch counts + connection lifecycle. */
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

describe("server-event subscriber", () => {
  it("opens no connection until an org is in scope", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    expect(FakeEventSource.instances.length).toBe(0);

    setOrgSlug("acme");
    expect(live().length).toBe(1);
    expect(live()[0]?.url).toBe("/api/sse/general?org=acme");
    expect(live()[0]?.withCredentials).toBe(true);
  });

  it("opens exactly one connection under repeated attach/slug reports (StrictMode-safe)", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    // Simulate a StrictMode double-invoke / route remount: same client + slug
    // reported again must not open a second stream.
    attachQueryClient(qc);
    setOrgSlug("acme");
    expect(FakeEventSource.instances.length).toBe(1);
  });

  it("re-targets the stream when the active org changes", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const first = FakeEventSource.instances[0];
    setOrgSlug("beta");

    expect(first?.closed).toBe(true);
    expect(live().length).toBe(1);
    expect(live()[0]?.url).toBe("/api/sse/general?org=beta");
  });

  it("closes the stream when the org leaves scope (logout / picker)", () => {
    const qc = new QueryClient();
    attachQueryClient(qc);
    setOrgSlug("acme");
    const first = FakeEventSource.instances[0];
    setOrgSlug(null);

    expect(first?.closed).toBe(true);
    expect(live().length).toBe(0);
  });

  it("coalesces a burst of events into a single invalidateQueries per key", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected one EventSource instance");

    for (let i = 0; i < 5; i++) {
      es.emit({ kind: "ticket_status_changed", ticket_id: "t1" });
    }

    expect(spy).not.toHaveBeenCalled();
    vi.advanceTimersByTime(250);

    // Distinct keys for this event: ["tickets"], ["tickets","t1"],
    // ["tickets","t1","audit"], ["reviewer","metrics"] = 4 invalidations.
    // Without coalescing this would be 5 * 4 = 20.
    expect(spy).toHaveBeenCalledTimes(4);
  });

  it("reconciles list caches on (re)connect to recover events missed before the stream opened", () => {
    // The EventSource opens asynchronously; any event published between connect
    // and the stream becoming OPEN is lost (Redis pub/sub has no replay). On
    // every (re)connect we refetch the list-level queries so a missed
    // ticket_status_changed still surfaces without a manual reload.
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected one EventSource instance");

    es.open();
    expect(spy).not.toHaveBeenCalled();
    vi.advanceTimersByTime(250);

    const invalidatedKeys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(invalidatedKeys).toContain(JSON.stringify(["tickets"]));
    expect(invalidatedKeys).toContain(JSON.stringify(["reviewer", "metrics"]));
  });

  it("invalidates the runs + overview + ticket keys on run_state_changed", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected one EventSource instance");

    es.emit({ kind: "run_state_changed", ticket_id: "t1", run_id: "r1", state: "paused" });
    vi.advanceTimersByTime(250);

    const invalidatedKeys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(invalidatedKeys).toContain(JSON.stringify(["runs", "t1"]));
    expect(invalidatedKeys).toContain(JSON.stringify(["runs", "overview", "t1"]));
    expect(invalidatedKeys).toContain(JSON.stringify(["tickets", "t1"]));
  });

  it("invalidates only the runs key on stage_state_changed", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected one EventSource instance");

    es.emit({ kind: "stage_state_changed", ticket_id: "t1", run_id: "r1" });
    vi.advanceTimersByTime(250);

    expect(spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))).toEqual([
      JSON.stringify(["runs", "t1"]),
    ]);
  });

  it("invalidates the artifacts key on artifact_stored", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected one EventSource instance");

    es.emit({ kind: "artifact_stored", ticket_id: "t1" });
    vi.advanceTimersByTime(250);

    expect(spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))).toEqual([
      JSON.stringify(["artifacts", "t1"]),
    ]);
  });

  it("ignores events with unparseable JSON without crashing", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    attachQueryClient(qc);
    setOrgSlug("acme");
    const es = live()[0];
    if (!es) throw new Error("expected one EventSource instance");
    es.onmessage?.({ data: "not-json" });
    vi.advanceTimersByTime(250);
    expect(spy).not.toHaveBeenCalled();
  });
});
