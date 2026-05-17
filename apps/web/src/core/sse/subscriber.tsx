import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useRef } from "react";
import type { ReviewJobActivityEvent } from "../api/client";
import { pushLiveActivity } from "./activity";
import type { ServerEvent } from "./types";

/** Mounted once at the app root. Opens an EventSource against `/api/events`
 * and translates each event into TanStack-Query cache invalidations so any
 * subscribed page refetches automatically.
 *
 * Translation table (`kind` → which queries to invalidate):
 * - `ticket_status_changed` → ["tickets"], ["tickets", ticket_id], ["tickets", ticket_id, "audit"], ["reviewer", "metrics"]
 * - `review_job_status_changed` → ["reviewer", "jobs", ticket_id], ["tickets", ticket_id, "audit"], ["reviewer", "metrics"], ["tickets"]
 * - `review_job_step_progress` → ["reviewer", "jobs", ticket_id] (in-place; no metrics / list churn)
 * - `review_job_activity` → in-memory ring buffer via `pushLiveActivity` (no
 *   query invalidation — too high-frequency; `useLiveActivity` reads the tail)
 *
 * Reconnection: native EventSource auto-reconnects on socket drop. We log
 * `onerror` for visibility but otherwise let the browser handle it.
 */
export function SSESubscriber({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    // Guard SSR / non-browser builds — EventSource is browser-only.
    if (typeof window === "undefined" || typeof EventSource === "undefined") return;

    const es = new EventSource("/api/events");
    esRef.current = es;

    es.onmessage = (msg) => {
      let evt: ServerEvent;
      try {
        evt = JSON.parse(msg.data);
      } catch {
        return;
      }
      const tid = evt.ticket_id;
      switch (evt.kind) {
        case "ticket_status_changed":
          qc.invalidateQueries({ queryKey: ["tickets"] });
          if (tid) {
            qc.invalidateQueries({ queryKey: ["tickets", tid] });
            qc.invalidateQueries({ queryKey: ["tickets", tid, "audit"] });
          }
          qc.invalidateQueries({ queryKey: ["reviewer", "metrics"] });
          break;
        case "review_job_status_changed":
          if (tid) {
            qc.invalidateQueries({ queryKey: ["reviewer", "jobs", tid] });
            qc.invalidateQueries({ queryKey: ["tickets", tid, "audit"] });
          }
          qc.invalidateQueries({ queryKey: ["reviewer", "metrics"] });
          qc.invalidateQueries({ queryKey: ["tickets"] });
          break;
        case "review_job_step_progress":
          // In-place row update; just refetch the jobs query for this ticket
          // so AgentCard.current_step swaps to the new label. No metrics /
          // ticket-list invalidation — only `current_step` moved.
          if (tid) {
            qc.invalidateQueries({ queryKey: ["reviewer", "jobs", tid] });
          }
          break;
        case "review_job_activity": {
          // High-frequency events from the coding-agent stream. Route into
          // the in-memory ring buffer so `useLiveActivity` rerenders the
          // open ticket without per-event query refetches.
          const reviewJobId = typeof evt.review_job_id === "string" ? evt.review_job_id : null;
          const activityEvent = (evt.event ?? null) as ReviewJobActivityEvent | null;
          if (reviewJobId && activityEvent) {
            pushLiveActivity(reviewJobId, activityEvent);
          }
          break;
        }
        default:
          // Unknown event kinds are tolerated — no invalidation, no error.
          break;
      }
    };

    es.onerror = () => {
      // Native EventSource auto-reconnects with backoff. We just log.
      if (es.readyState === EventSource.CLOSED) {
        // Closed (e.g., backend down). Browser will keep retrying.
      }
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [qc]);

  return <>{children}</>;
}
