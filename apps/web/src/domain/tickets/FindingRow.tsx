/**
 * Finding row — non-interactive display for the canonical finding schema.
 *
 * Renders one finding with severity/confidence/category chips, file:line
 * anchor (when present), rationale, and suggested fix. No ack / push-back
 * actions — findings are read-only in the current review UI.
 *
 * Source of truth: the `FindingRow` shape returned by
 * `/api/reviewer/findings/by-ticket/:ticket_id`.
 */

import type { FindingRow as FindingRowData } from "@core/api/public/queries";
import { cn } from "@shared/utils/public/cn";

interface SeverityMeta {
  label: string;
  chip: string;
}

// Solid semantic-color chips pass WCAG AA contrast against the matching
// `*-foreground` pair (see TicketsListPage for the same rationale).
const DEFAULT_SEVERITY: SeverityMeta = {
  label: "Should Fix",
  chip: "bg-warning text-warning-foreground border-warning",
};

const SEVERITY_META: Record<string, SeverityMeta> = {
  blocker: {
    label: "Blocker",
    chip: "bg-destructive text-destructive-foreground border-destructive",
  },
  should_fix: DEFAULT_SEVERITY,
  nit: { label: "Nit", chip: "bg-muted text-muted-foreground border-border" },
};

const CONFIDENCE_META: Record<string, { label: string; chip: string }> = {
  verified: { label: "Verified", chip: "bg-success text-success-foreground border-success" },
  plausible: { label: "Plausible", chip: "bg-info text-info-foreground border-info" },
  speculative: { label: "Speculative", chip: "bg-muted text-muted-foreground border-border" },
};

interface FindingRowProps {
  finding: FindingRowData;
}

export function FindingRow({ finding }: FindingRowProps) {
  const severity = SEVERITY_META[finding.severity] ?? DEFAULT_SEVERITY;
  const confidence = CONFIDENCE_META[finding.confidence] ?? {
    label: finding.confidence,
    chip: "bg-muted text-muted-foreground border-border",
  };

  // Derive a short display headline from the first sentence of rationale
  // (the canonical schema has no separate `title` field).
  const headline =
    finding.rationale.split(/[.!?]/)[0]?.trim() ?? finding.rule_violated ?? "Finding";

  return (
    <article
      data-testid={`finding-row-${finding.id}`}
      className="rounded-md border border-border p-3 flex flex-col gap-2"
    >
      <header className="flex items-baseline gap-2 flex-wrap">
        <span
          className={cn(
            "inline-flex items-center px-1.5 h-5 rounded text-[10.5px] font-medium border uppercase tracking-wider",
            severity.chip,
          )}
          data-testid={`finding-severity-${finding.id}`}
        >
          {severity.label}
        </span>
        <span
          className={cn(
            "inline-flex items-center px-1.5 h-5 rounded text-[10.5px] font-medium border uppercase tracking-wider",
            confidence.chip,
          )}
          data-testid={`finding-confidence-${finding.id}`}
        >
          {confidence.label}
        </span>
        <span className="text-xs font-mono text-muted-foreground">{finding.category}</span>
        <span className="font-medium text-sm flex-1">{headline}</span>
        {finding.file && (
          <span className="text-xs text-muted-foreground mono">
            {finding.file}
            {finding.line != null ? `:${finding.line}` : ""}
          </span>
        )}
      </header>

      {finding.rationale && (
        <p className="text-xs text-muted-foreground whitespace-pre-wrap">{finding.rationale}</p>
      )}

      {finding.suggested_fix && (
        <div className="text-xs text-foreground bg-secondary rounded p-2 whitespace-pre-wrap">
          <span className="font-medium text-muted-foreground">Suggested fix: </span>
          {finding.suggested_fix}
        </div>
      )}

      <footer className="flex items-center gap-2 text-xs text-muted-foreground">
        <span className="font-mono">{finding.rule_violated}</span>
        {finding.rule_source && (
          <>
            <span>·</span>
            <span>{finding.rule_source}</span>
          </>
        )}
      </footer>
    </article>
  );
}
