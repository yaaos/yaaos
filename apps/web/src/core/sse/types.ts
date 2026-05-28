/** Server-sent event from yaaos's `/api/sse/general` stream.
 *
 * The backend serializes `GeneralEventKind`-typed payloads into SSE frames.
 * Each frame carries:
 * - `kind`: discriminator (e.g. "ticket_status_changed", "review_job_status_changed")
 * - `source_module`: which domain module published
 * - `ts`: ISO timestamp
 * - `ticket_id`: optional, present for ticket-scoped events
 *
 * The FE treats the payload as opaque except for `kind` and `ticket_id` —
 * translation to cache invalidations lives in `subscriber.ts`.
 */
export type ServerEvent = {
  kind: string;
  source_module: string;
  ts: string;
  ticket_id: string | null;
  // Domain modules add extra fields per kind; we read them dynamically.
  [extra: string]: unknown;
};
