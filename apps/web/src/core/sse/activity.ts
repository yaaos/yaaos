/** Module-level ring buffer for live `review_job_activity` SSE events.
 *
 * Volume is per-review on the order of ~100 events over minutes; rather than
 * thrashing TanStack-Query refetches per event, the SSE subscriber writes
 * directly into this buffer and `useLiveActivity` reads from it via a tiny
 * subscribe-with-snapshot pattern. The persisted history lives in
 * `ReviewJob.activity_log` and is merged with the live tail at render-time.
 */

import { useSyncExternalStore } from "react";
import type { ReviewJobActivityEvent } from "../api/client";

const RING_CAP = 200;

const _buffers = new Map<string, ReviewJobActivityEvent[]>();
const _listeners = new Set<() => void>();

/** Append a live event for `reviewJobId`. Older entries roll off above {@link RING_CAP}. */
export function pushLiveActivity(reviewJobId: string, event: ReviewJobActivityEvent): void {
  const cur = _buffers.get(reviewJobId) ?? [];
  // Clone the array so `useSyncExternalStore` sees a new reference.
  const next = cur.length >= RING_CAP ? [...cur.slice(1), event] : [...cur, event];
  _buffers.set(reviewJobId, next);
  for (const fn of _listeners) fn();
}

/** Snapshot the live tail for a review job. Returns `[]` if none yet. */
export function getLiveActivity(reviewJobId: string): ReviewJobActivityEvent[] {
  return _buffers.get(reviewJobId) ?? _EMPTY;
}

const _EMPTY: ReviewJobActivityEvent[] = [];

function subscribe(fn: () => void): () => void {
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}

/** Read the live SSE-fed activity tail for a review job. Stable empty array
 * is returned when no events have arrived yet, so the merged-with-persisted
 * shape stays referentially predictable. */
export function useLiveActivity(reviewJobId: string): ReviewJobActivityEvent[] {
  return useSyncExternalStore(
    subscribe,
    () => getLiveActivity(reviewJobId),
    () => _EMPTY,
  );
}

/** Test/dev helper — clears the ring for all review jobs. */
export function _resetLiveActivityForTests(): void {
  _buffers.clear();
}
