/** Per-workflow activity-stream SSE hook (demand-pull).
 *
 * Opens an `EventSource` to `/api/sse/workspace_activity/{id}` when
 * mounted; closes on unmount. The connection lifecycle drives the
 * backend's `SubscriberRegistry.track/untrack`, which sends
 * `subscribe`/`unsubscribe` over the agent WebSocket — closing the tab
 * cuts off the WorkspaceAgent's activity batches all the way through.
 */

import { getCurrentOrgSlug } from "@core/api/public/org-context";
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
    // `/api/sse` is org-scoped, but EventSource can't send X-Yaaos-Org-Slug — the
    // slug rides in the `?org=` query param instead (backend accepts it for
    // SSE routes). Read it from the URL; the hook only runs inside an org.
    const slug = getCurrentOrgSlug();
    const url = slug
      ? `/api/sse/workspace_activity/${workflowExecutionId}?org=${encodeURIComponent(slug)}`
      : `/api/sse/workspace_activity/${workflowExecutionId}`;
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
