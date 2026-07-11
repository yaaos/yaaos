/**
 * Auto-save for an existing pipeline definition. Every committed edit PUTs
 * the whole definition immediately; PUTs are serialized — while one is in
 * flight the newest committed draft waits in a queue slot and is sent when
 * the in-flight response lands, so responses can't arrive out of order.
 * Invalid drafts never PUT ("blocked"); a failed PUT keeps the local draft,
 * and the next commit retries the full body.
 */

import { type PipelineDetailView, useUpdatePipeline } from "@core/api/public/queries";
import { useCallback, useEffect, useRef, useState } from "react";
import { type PipelineDraft, draftToWire, pipelineDraftIsValid } from "./types";

type SaveStatus = "idle" | "saving" | "saved" | "blocked" | "error";

export function useAutoSavePipeline(
  pipelineId: string,
  opts: { onSaved: (detail: PipelineDetailView) => void },
): {
  commit: (draft: PipelineDraft) => void;
  markSaved: (draft: PipelineDraft) => void;
  status: SaveStatus;
  error: unknown;
} {
  const update = useUpdatePipeline();
  const [status, setStatus] = useState<SaveStatus>("idle");
  const [error, setError] = useState<unknown>(null);

  const lastSavedBodyRef = useRef<string | null>(null);
  // Body of the PUT currently on the wire; null when idle. The truth an
  // in-flight response asserts is only as new as this body — commit() and
  // the settle handlers compare against it so an older response can never
  // override a newer local draft.
  const inFlightBodyRef = useRef<string | null>(null);
  const queuedRef = useRef<{ draft: PipelineDraft; body: string } | null>(null);
  // True while the newest commit was invalid — a "blocked" verdict outlives
  // any older in-flight PUT's success, so "Saved." can't paper over an
  // unsaved invalid draft.
  const blockedRef = useRef(false);
  const hasSavedOnceRef = useRef(false);
  const onSavedRef = useRef(opts.onSaved);
  useEffect(() => {
    onSavedRef.current = opts.onSaved;
  });

  const mutate = update.mutate;

  const startPut = useCallback(
    function startPut(draft: PipelineDraft, body: string): void {
      inFlightBodyRef.current = body;
      setStatus("saving");
      mutate(
        { id: pipelineId, definition: draftToWire(draft) },
        {
          onSuccess: (detail) => {
            lastSavedBodyRef.current = body;
            hasSavedOnceRef.current = true;
            const queued = queuedRef.current;
            if (queued) {
              // Stale response — a newer draft is waiting; send it instead.
              queuedRef.current = null;
              startPut(queued.draft, queued.body);
              return;
            }
            inFlightBodyRef.current = null;
            if (blockedRef.current) return;
            setStatus("saved");
            onSavedRef.current(detail);
          },
          onError: (err) => {
            inFlightBodyRef.current = null;
            queuedRef.current = null;
            if (blockedRef.current) return;
            setError(err);
            setStatus("error");
          },
        },
      );
    },
    [mutate, pipelineId],
  );

  const commit = useCallback(
    (draft: PipelineDraft): void => {
      const body = JSON.stringify(draftToWire(draft));
      if (body === lastSavedBodyRef.current) {
        blockedRef.current = false;
        if (inFlightBodyRef.current != null && inFlightBodyRef.current !== body) {
          // Revert while an older draft is on the wire — the in-flight PUT
          // will overwrite the server with the older draft, so this body
          // must be re-sent to converge on what the user sees.
          queuedRef.current = { draft, body };
          setStatus("saving");
          return;
        }
        queuedRef.current = null;
        // Nothing to persist; clear a stale blocked/error status.
        setStatus((prev) =>
          prev === "blocked" || prev === "error"
            ? hasSavedOnceRef.current
              ? "saved"
              : "idle"
            : prev,
        );
        return;
      }
      if (!pipelineDraftIsValid(draft)) {
        blockedRef.current = true;
        queuedRef.current = null;
        setStatus("blocked");
        return;
      }
      blockedRef.current = false;
      if (inFlightBodyRef.current != null) {
        // Coalesce — only the newest queued draft survives.
        queuedRef.current = { draft, body };
        setStatus("saving");
        return;
      }
      startPut(draft, body);
    },
    [startPut],
  );

  const markSaved = useCallback((draft: PipelineDraft): void => {
    lastSavedBodyRef.current = JSON.stringify(draftToWire(draft));
  }, []);

  return { commit, markSaved, status, error };
}
