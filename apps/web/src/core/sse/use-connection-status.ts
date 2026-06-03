import { useSyncExternalStore } from "react";
import type { ConnectionStatus } from "./subscriber";
import { getSnapshot, subscribe } from "./subscriber";

/** Returns the current SSE connection status (`idle` | `connecting` |
 * `connected` | `disconnected`) as a tear-free, concurrent-safe React value.
 *
 * Backed by the module-scope store via `useSyncExternalStore`. Renderable
 * anywhere — components can show "reconnecting…" without polling. */
export function useConnectionStatus(): ConnectionStatus {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot).status;
}
