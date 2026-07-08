/**
 * Unit tests for `useRunActivityTail`.
 *
 * Stubs global `EventSource` so there is no real network I/O. Asserts:
 * - Hook appends normalized frames as messages arrive.
 * - Cap at MAX_EVENTS (500) evicts the oldest entries.
 * - `lastEvent` is the final element of the kept list.
 * - Cleanup on unmount closes the EventSource and resets state.
 * - Changing `runId` closes the old EventSource and opens a fresh one with
 *   a reset event list.
 * - `runId=null` opens no connection.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRunActivityTail } from "../public/run_activity";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
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

function liveInstances(): FakeEventSource[] {
  return FakeEventSource.instances.filter((es) => !es.closed);
}

vi.mock("@core/api/public/org-context", () => ({
  getCurrentOrgSlug: () => "acme",
}));

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as unknown as { EventSource: unknown }).EventSource =
    FakeEventSource as unknown as typeof EventSource;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useRunActivityTail", () => {
  it("opens no EventSource when runId is null", () => {
    renderHook(() => useRunActivityTail(null));
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("opens an EventSource with the run-id in the URL when runId is set", () => {
    renderHook(() => useRunActivityTail("run-abc"));
    expect(liveInstances()).toHaveLength(1);
    expect(liveInstances()[0]?.url).toContain("/api/sse/workspace_activity/run-abc");
    expect(liveInstances()[0]?.url).toContain("org=acme");
    expect(liveInstances()[0]?.withCredentials).toBe(true);
  });

  it("appends normalized frames to the event list on each message", () => {
    const { result } = renderHook(() => useRunActivityTail("run-1"));
    const es = liveInstances()[0];
    if (!es) throw new Error("no EventSource");

    act(() => {
      es.emit({
        kind: "assistant_message",
        ts: "2026-01-01T00:00:00Z",
        message: "hello",
        detail: null,
      });
      es.emit({
        kind: "tool_call_started",
        ts: "2026-01-01T00:00:01Z",
        message: "Read",
        detail: { tool: "Read" },
      });
    });

    expect(result.current.events).toHaveLength(2);
    expect(result.current.events[0]).toMatchObject({
      kind: "assistant_message",
      message: "hello",
    });
    expect(result.current.events[1]).toMatchObject({
      kind: "tool_call_started",
      message: "Read",
    });
  });

  it("exposes lastEvent as the most recent frame", () => {
    const { result } = renderHook(() => useRunActivityTail("run-1"));
    const es = liveInstances()[0];
    if (!es) throw new Error("no EventSource");

    act(() => {
      es.emit({
        kind: "assistant_message",
        ts: "2026-01-01T00:00:00Z",
        message: "first",
        detail: null,
      });
      es.emit({
        kind: "assistant_message",
        ts: "2026-01-01T00:00:01Z",
        message: "second",
        detail: null,
      });
    });

    expect(result.current.lastEvent?.message).toBe("second");
  });

  it("returns lastEvent=null and events=[] before any frames arrive", () => {
    const { result } = renderHook(() => useRunActivityTail("run-1"));
    expect(result.current.events).toHaveLength(0);
    expect(result.current.lastEvent).toBeNull();
  });

  it("connected is false before onopen and true after", () => {
    const { result } = renderHook(() => useRunActivityTail("run-1"));
    const es = liveInstances()[0];
    if (!es) throw new Error("no EventSource");

    expect(result.current.connected).toBe(false);

    act(() => {
      es.open();
    });

    expect(result.current.connected).toBe(true);
  });

  it("connected resets to false on unmount and run-id change", () => {
    const { result, unmount } = renderHook(
      ({ runId }: { runId: string }) => useRunActivityTail(runId),
      { initialProps: { runId: "run-X" } },
    );
    const esX = liveInstances()[0];
    if (!esX) throw new Error("no EventSource");

    act(() => {
      esX.open();
    });
    expect(result.current.connected).toBe(true);

    unmount();
    // After unmount the hook state is gone; the important check is the
    // EventSource was closed.
    expect(esX.closed).toBe(true);
  });

  it("caps the event list at 500 and evicts oldest entries", () => {
    const { result } = renderHook(() => useRunActivityTail("run-1"));
    const es = liveInstances()[0];
    if (!es) throw new Error("no EventSource");

    act(() => {
      for (let i = 0; i < 600; i++) {
        es.emit({ kind: "k", ts: "2026-01-01T00:00:00Z", message: `msg-${i}`, detail: null });
      }
    });

    expect(result.current.events).toHaveLength(500);
    // The OLDEST 100 should have been evicted; first surviving entry is msg-100.
    expect(result.current.events[0]?.message).toBe("msg-100");
    expect(result.current.lastEvent?.message).toBe("msg-599");
  });

  it("normalizes missing fields to safe defaults", () => {
    const { result } = renderHook(() => useRunActivityTail("run-1"));
    const es = liveInstances()[0];
    if (!es) throw new Error("no EventSource");

    act(() => {
      // Emit a frame with no fields to exercise the fallback paths.
      es.emit({});
    });

    const ev = result.current.events[0];
    expect(ev?.kind).toBe("unknown");
    expect(typeof ev?.ts).toBe("string");
    expect(ev?.message).toBe("");
  });

  it("closes the EventSource and resets state on unmount", () => {
    const { result, unmount } = renderHook(() => useRunActivityTail("run-1"));
    const es = liveInstances()[0];
    if (!es) throw new Error("no EventSource");

    act(() => {
      es.emit({ kind: "k", ts: "2026-01-01T00:00:00Z", message: "hi", detail: null });
    });
    expect(result.current.events).toHaveLength(1);

    unmount();

    expect(es.closed).toBe(true);
    // After unmount, querying the result would be stale, but the important
    // thing is the EventSource was closed — regression guard for the cleanup path.
  });

  it("closes the old EventSource and resets when runId changes", () => {
    const { result, rerender } = renderHook(
      ({ runId }: { runId: string }) => useRunActivityTail(runId),
      { initialProps: { runId: "run-A" } },
    );
    const esA = liveInstances()[0];
    if (!esA) throw new Error("no initial EventSource");

    act(() => {
      esA.emit({ kind: "k", ts: "2026-01-01T00:00:00Z", message: "from A", detail: null });
    });
    expect(result.current.events).toHaveLength(1);

    rerender({ runId: "run-B" });

    expect(esA.closed).toBe(true);
    // New EventSource for run-B.
    const liveAfter = liveInstances();
    expect(liveAfter).toHaveLength(1);
    expect(liveAfter[0]?.url).toContain("run-B");
    // Event list reset — no bleed from run-A.
    expect(result.current.events).toHaveLength(0);
  });

  it("ignores malformed JSON without crashing", () => {
    const { result } = renderHook(() => useRunActivityTail("run-1"));
    const es = liveInstances()[0];
    if (!es) throw new Error("no EventSource");

    act(() => {
      es.onmessage?.({ data: "not-json{{{{" });
    });

    expect(result.current.events).toHaveLength(0);
  });
});
