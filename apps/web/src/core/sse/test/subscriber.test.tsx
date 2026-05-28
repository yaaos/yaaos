import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SSESubscriber, _resetSSESubscriberForTests } from "../subscriber";

/** Captured per-test so we can verify dispatch counts + StrictMode behavior. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
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

function wrap(qc: QueryClient) {
  return (
    <StrictMode>
      <QueryClientProvider client={qc}>
        <SSESubscriber>{null}</SSESubscriber>
      </QueryClientProvider>
    </StrictMode>
  );
}

describe("SSESubscriber", () => {
  it("opens exactly one EventSource even under StrictMode double-mount", () => {
    const qc = new QueryClient();
    render(wrap(qc));
    expect(FakeEventSource.instances.length).toBe(1);
    expect(FakeEventSource.instances[0]?.url).toBe("/api/sse/general");
    expect(FakeEventSource.instances[0]?.withCredentials).toBe(true);
  });

  it("coalesces a burst of events into a single invalidateQueries per key", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    render(wrap(qc));
    const [es] = FakeEventSource.instances;
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

  it("ignores events with unparseable JSON without crashing", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    render(wrap(qc));
    const [es] = FakeEventSource.instances;
    if (!es) throw new Error("expected one EventSource instance");
    es.onmessage?.({ data: "not-json" });
    vi.advanceTimersByTime(250);
    expect(spy).not.toHaveBeenCalled();
  });
});
