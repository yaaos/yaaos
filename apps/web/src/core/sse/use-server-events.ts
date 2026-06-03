import { useSyncExternalStore } from "react";
import { getSnapshot, subscribe } from "./subscriber";
import type { SSESnapshot } from "./subscriber";

/** Returns the current SSE store snapshot (`status` + `lastEvent`) with
 * tear-free, concurrent-safe reads via `useSyncExternalStore`.
 *
 * Call `useServerEvents()` in `AppShell` (the root owner that also wires up
 * the `QueryClient` and org slug). Other consumers that only need to _read_
 * connection state should use `useConnectionStatus()` instead. */
export function useSSESnapshot(): SSESnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
