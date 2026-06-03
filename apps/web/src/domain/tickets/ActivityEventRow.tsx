/**
 * Activity event row — one event in the Activity tab live stream.
 *
 * Pure-render: takes a `ReviewJobActivityEvent` from `@core/api` + an
 * optional `expanded` flag. Maps the freeform `kind` string to a lucide
 * icon per the baseline taxonomy:
 *
 *   session_start         → Play
 *   subagent_dispatched   → GitBranch
 *   tool_call_started     → Wrench
 *   tool_call_finished    → CheckCircle2 / XCircle  (by detail.exit_code)
 *   assistant_message     → MessageSquare
 *   result                → Flag
 *   anything else         → Circle (fallback)
 *
 * The `message` field is backend-rendered (the FE never interprets raw
 * Claude shapes); `detail` carries kind-specific extras for the expanded
 * body. Long messages auto-collapse to 3 lines per the activity-stream
 * spec; the wrapper `<details>` element drives expansion.
 */

import type { ReviewJobActivityEvent } from "@core/api";
import { ago } from "@shared/utils/ago";
import { cn } from "@shared/utils/cn";
import {
  CheckCircle2,
  Circle,
  Flag,
  GitBranch,
  type LucideIcon,
  MessageSquare,
  Play,
  Wrench,
  XCircle,
} from "lucide-react";

interface IconMeta {
  Icon: LucideIcon;
  tone: string;
}

const KIND_META: Record<string, IconMeta> = {
  session_start: { Icon: Play, tone: "text-info" },
  subagent_dispatched: { Icon: GitBranch, tone: "text-muted-foreground" },
  tool_call_started: { Icon: Wrench, tone: "text-muted-foreground" },
  assistant_message: { Icon: MessageSquare, tone: "text-foreground" },
  result: { Icon: Flag, tone: "text-success" },
};

function metaFor(event: ReviewJobActivityEvent): IconMeta {
  if (event.kind === "tool_call_finished") {
    const exit = event.detail?.exit_code;
    return exit === 0
      ? { Icon: CheckCircle2, tone: "text-success" }
      : { Icon: XCircle, tone: "text-destructive" };
  }
  return KIND_META[event.kind] ?? { Icon: Circle, tone: "text-muted-foreground" };
}

interface ActivityEventRowProps {
  event: ReviewJobActivityEvent;
  className?: string;
}

export function ActivityEventRow({ event, className }: ActivityEventRowProps) {
  const { Icon, tone } = metaFor(event);
  const longMessage = event.message.split("\n").length > 3 || event.message.length > 240;

  return (
    <article
      data-testid="activity-event-row"
      data-kind={event.kind}
      className={cn(
        "flex items-start gap-2.5 px-3 py-2 border-b border-border last:border-0 text-sm",
        className,
      )}
    >
      <Icon className={cn("w-3.5 h-3.5 shrink-0 mt-1", tone)} aria-hidden="true" />
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-xs text-muted-foreground mono shrink-0">{ago(event.ts)}</span>
          <span className="text-xs text-muted-foreground mono shrink-0">{event.kind}</span>
        </div>
        {longMessage ? (
          <details className="mt-0.5">
            <summary
              className={cn(
                "cursor-pointer text-foreground line-clamp-3 whitespace-pre-wrap",
                "marker:text-muted-foreground",
              )}
            >
              {event.message}
            </summary>
            <div className="mt-1 whitespace-pre-wrap text-foreground">{event.message}</div>
          </details>
        ) : (
          <div className="mt-0.5 text-foreground whitespace-pre-wrap">{event.message}</div>
        )}
      </div>
    </article>
  );
}
