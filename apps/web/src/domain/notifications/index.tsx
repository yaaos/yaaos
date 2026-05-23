/**
 * Notifications — full page (E2a.6).
 *
 * Cross-org chronological list. Backed by `useNotifications()` →
 * GET /api/notifications. Row click marks-as-read via the per-row
 * mutation; "Mark all read" hits POST /api/notifications/mark-read.
 *
 * SSE wiring (`notification_created` / `notification_read` invalidations)
 * lands once the workflow engine emits those kinds — Phase 7 polish pass.
 */

import {
  type Notification as NotificationItem,
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
} from "@core/api";
import { EmptyState, PageHeader } from "@shared/components/layout";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/ago";
import { cn } from "@shared/utils/cn";
import { Bell } from "lucide-react";
import { useState } from "react";

type ReadFilter = "all" | "unread" | "read";

export function NotificationsPage() {
  const [filter, setFilter] = useState<ReadFilter>("all");
  const { data: items, isLoading } = useNotifications(filter);
  const markOne = useMarkNotificationRead();
  const markAll = useMarkAllNotificationsRead();

  return (
    <div className="mx-auto max-w-[900px] px-6 py-8">
      <PageHeader
        title="Notifications"
        subtitle="Cross-org inbox."
        actions={
          <Button variant="outline" onClick={() => markAll.mutate()} disabled={markAll.isPending}>
            Mark all read
          </Button>
        }
      />

      <div className="flex gap-1 mb-4">
        {(["all", "unread", "read"] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setFilter(k)}
            data-testid={`notifications-filter-${k}`}
            aria-pressed={filter === k}
            className={cn(
              "px-2.5 h-7 rounded-full text-xs font-medium border transition-colors",
              filter === k
                ? "bg-primary/10 text-primary border-primary/30"
                : "bg-secondary text-muted-foreground border-border hover:text-foreground",
            )}
          >
            {k[0]?.toUpperCase() + k.slice(1)}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 5 }).map((_, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: skeletons
            <Skeleton key={i} className="h-14" />
          ))}
        </div>
      ) : !items || items.length === 0 ? (
        <EmptyState
          icon={Bell}
          headline="No notifications."
          body="When yaaos needs a decision on one of your tickets, or finishes a review, it shows up here."
        />
      ) : (
        <ul
          className="rounded-md border border-border overflow-hidden"
          data-testid="notifications-list"
        >
          {items.map((n) => (
            <Row key={n.id} item={n} onClick={() => markOne.mutate(n.id)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function Row({ item, onClick }: { item: NotificationItem; onClick: () => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        data-testid={`notification-row-${item.id}`}
        className={cn(
          "flex flex-col items-start gap-1 w-full px-4 py-3 border-b border-border last:border-0 text-left",
          "hover:bg-accent hover:text-accent-foreground transition-colors",
          !item.read_at && "bg-accent/40",
        )}
      >
        <div className="flex w-full items-baseline gap-3">
          <span className={cn("flex-1 truncate text-sm", !item.read_at && "font-medium")}>
            {item.title}
          </span>
          <span className="text-xs text-muted-foreground shrink-0">{ago(item.created_at)}</span>
        </div>
        <p className="text-xs text-muted-foreground">{item.body}</p>
      </button>
    </li>
  );
}
