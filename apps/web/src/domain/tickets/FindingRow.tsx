/**
 * Finding row — inline ack / push-back actions per E2a.4.
 *
 * Renders one finding with severity pill, title, file:line, body excerpt,
 * and (when the finding is `open`) Ack + Push back buttons. Push back opens
 * an inline textarea for the required reason. Builders see all controls
 * (Builder = full action access per A1).
 *
 * Source of truth: the `FindingRow` shape returned by
 * `/api/reviewer/findings/by-ticket/:ticket_id`.
 */

import type { FindingRow as FindingRowData } from "@core/api";
import { Button } from "@shared/components/ui/button";
import { Textarea } from "@shared/components/ui/textarea";
import { cn } from "@shared/utils";
import { useState } from "react";

interface SeverityMeta {
  label: string;
  chip: string;
}

// Solid semantic-color chips pass WCAG AA contrast against the matching
// `*-foreground` pair (same fix as TicketsListPage status chips).
const DEFAULT_SEVERITY: SeverityMeta = {
  label: "Minor",
  chip: "bg-info text-info-foreground border-info",
};

const SEVERITY_META: Record<string, SeverityMeta> = {
  blocker: {
    label: "Blocker",
    chip: "bg-destructive text-destructive-foreground border-destructive",
  },
  major: { label: "Major", chip: "bg-warning text-warning-foreground border-warning" },
  minor: DEFAULT_SEVERITY,
  nit: { label: "Nit", chip: "bg-muted text-muted-foreground border-border" },
};

interface FindingRowProps {
  finding: FindingRowData;
  onAck?: (finding_id: string) => void;
  onPushBack?: (args: { finding_id: string; reason: string }) => void;
  pending?: boolean;
}

export function FindingRow({ finding, onAck, onPushBack, pending }: FindingRowProps) {
  const [pushOpen, setPushOpen] = useState(false);
  const [reason, setReason] = useState("");

  const severity = SEVERITY_META[finding.severity] ?? DEFAULT_SEVERITY;
  const isOpen = finding.state === "open";
  const stateLabel = stateLabelFor(finding.state);

  const submitPushBack = () => {
    const trimmed = reason.trim();
    if (trimmed.length < 10) return;
    onPushBack?.({ finding_id: finding.id, reason: trimmed });
  };

  return (
    <article
      data-testid={`finding-row-${finding.id}`}
      data-state={finding.state}
      className={cn(
        "rounded-md border border-border p-3 flex flex-col gap-2",
        finding.state !== "open" && "opacity-70",
      )}
    >
      <header className="flex items-baseline gap-2 flex-wrap">
        <span
          className={cn(
            "inline-flex items-center px-1.5 h-5 rounded text-[10.5px] font-medium border uppercase tracking-wider",
            severity.chip,
          )}
        >
          {severity.label}
        </span>
        <span className="font-medium text-sm">{finding.title}</span>
        <span className="text-xs text-muted-foreground mono">
          {finding.file_path}:{finding.line_start}
        </span>
        {!isOpen && <span className="ml-auto text-xs text-muted-foreground">{stateLabel}</span>}
      </header>

      <p className="text-xs text-muted-foreground whitespace-pre-wrap">{finding.body}</p>

      {isOpen && (
        <footer className="flex items-center gap-2">
          {!pushOpen && (
            <>
              <Button
                variant="outline"
                onClick={() => onAck?.(finding.id)}
                disabled={pending}
                data-testid={`finding-ack-${finding.id}`}
              >
                Ack
              </Button>
              <Button
                variant="ghost"
                onClick={() => setPushOpen(true)}
                disabled={pending}
                data-testid={`finding-pushback-toggle-${finding.id}`}
              >
                Push back
              </Button>
            </>
          )}
          {pushOpen && (
            <div className="flex-1 flex flex-col gap-2">
              <Textarea
                data-testid={`finding-pushback-reason-${finding.id}`}
                placeholder="Reason (≥10 characters)…"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={2}
              />
              <div className="flex items-center justify-end gap-2">
                <Button
                  variant="ghost"
                  onClick={() => {
                    setPushOpen(false);
                    setReason("");
                  }}
                  disabled={pending}
                >
                  Cancel
                </Button>
                <Button
                  onClick={submitPushBack}
                  disabled={pending || reason.trim().length < 10}
                  data-testid={`finding-pushback-submit-${finding.id}`}
                >
                  Submit push-back
                </Button>
              </div>
            </div>
          )}
        </footer>
      )}
    </article>
  );
}

function stateLabelFor(state: FindingRowData["state"]): string {
  switch (state) {
    case "acknowledged":
      return "Acked";
    case "resolved_confirmed":
      return "Resolved";
    case "resolved_unverified":
      return "Resolved (unverified)";
    case "stale":
      return "Stale";
    default:
      return state;
  }
}
