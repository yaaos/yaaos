/**
 * Compact elapsed-time formatter for run/stage timing. `end === null` means
 * still in flight — elapsed is computed against `Date.now()` at render time
 * (no timer/ticker; SSE-driven invalidations re-render the caller).
 * Returns "42s" / "3m 12s" / "1h 04m" / "—" for an unparseable timestamp.
 */
export function duration(start: string | null | undefined, end: string | null | undefined): string {
  if (!start) return "—";
  const startMs = new Date(start).getTime();
  if (Number.isNaN(startMs)) return "—";
  const endMs = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(endMs)) return "—";

  const totalSec = Math.max(0, Math.floor((endMs - startMs) / 1000));
  const hours = Math.floor(totalSec / 3600);
  const minutes = Math.floor((totalSec % 3600) / 60);
  const seconds = totalSec % 60;

  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}
