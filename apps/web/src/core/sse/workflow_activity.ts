/** Per-workflow activity-stream SSE hook (demand-pull).
 *
 * Opens an `EventSource` to `/api/workspaces/workflows/{id}/activity` when
 * mounted; closes on unmount. The connection lifecycle drives the
 * backend's `SubscriberRegistry.track/untrack`, which sends
 * `subscribe`/`unsubscribe` over the agent WebSocket — closing the tab
 * cuts off the WorkspaceAgent's activity batches all the way through.
 *
 * Replaces the legacy global `/api/events` ring-buffer model for
 * activity events (the global stream still carries other event kinds).
 */

import { useEffect, useState } from "react";

export interface WorkflowActivityEvent {
  kind: string;
  ts: string;
  message: string;
  detail?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export function useWorkflowActivityStream(
  workflowExecutionId: string | null | undefined,
): WorkflowActivityEvent[] {
  const [events, setEvents] = useState<WorkflowActivityEvent[]>([]);

  useEffect(() => {
    if (!workflowExecutionId) return;
    const url = `/api/workspaces/workflows/${workflowExecutionId}/activity`;
    const es = new EventSource(url, { withCredentials: true });
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as Partial<WorkflowActivityEvent>;
        const normalized: WorkflowActivityEvent = {
          kind: typeof data.kind === "string" ? data.kind : "unknown",
          ts: typeof data.ts === "string" ? data.ts : new Date().toISOString(),
          message: typeof data.message === "string" ? data.message : "",
          detail:
            typeof data.detail === "object" && data.detail !== null
              ? (data.detail as Record<string, unknown>)
              : null,
        };
        setEvents((prev) => [...prev, normalized]);
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
    };
  }, [workflowExecutionId]);

  return events;
}
