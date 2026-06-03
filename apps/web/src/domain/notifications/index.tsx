import {
  type Notification as NotificationItem,
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
} from "@core/api";
import { EmptyState, ErrorBanner, PageHeader } from "@shared/components/layout";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago, cn } from "@shared/utils";
import { Bell } from "lucide-react";
import { Suspense } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { type ReadFilter, useNotificationsFilter } from "./use-notifications-filter";

export function NotificationsPage() {
  const { filter, setFilter } = useNotificationsFilter();

  return (
    <div className="mx-auto max-w-[900px] px-6 py-8">
      <PageHeader title="Notifications" subtitle="Cross-org inbox." actions={<MarkAllButton />} />

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

      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load notifications." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div className="flex flex-col gap-2">
              {Array.from({ length: 5 }).map((_, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: skeletons
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          }
        >
          <NotificationsList filter={filter} />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}

function MarkAllButton() {
  const markAll = useMarkAllNotificationsRead();
  return (
    <Button variant="outline" onClick={() => markAll.mutate()} disabled={markAll.isPending}>
      Mark all read
    </Button>
  );
}

function NotificationsList({ filter }: { filter: ReadFilter }) {
  const { data: items } = useNotifications(filter);
  const markOne = useMarkNotificationRead();

  if (!items || items.length === 0) {
    return (
      <EmptyState
        icon={Bell}
        headline="No notifications."
        body="When yaaos needs a decision on one of your tickets, or finishes a review, it shows up here."
      />
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="notifications-list">
      {groupByDate(items).map((group) => (
        <section key={group.label}>
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">
            {group.label}
          </h2>
          <ul className="rounded-md border border-border overflow-hidden">
            {group.items.map((n) => (
              <Row key={n.id} item={n} onClick={() => markOne.mutate(n.id)} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

interface DateGroup {
  label: string;
  items: NotificationItem[];
}

/**
 * Bucket notifications into four date groups: Today, Yesterday, This week,
 * Older. Preserves server ordering inside each bucket (newest first).
 */
function groupByDate(items: NotificationItem[]): DateGroup[] {
  const groups: DateGroup[] = [
    { label: "Today", items: [] },
    { label: "Yesterday", items: [] },
    { label: "This week", items: [] },
    { label: "Older", items: [] },
  ];
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfYesterday = startOfToday - 86_400_000;
  const startOfWeek = startOfToday - 6 * 86_400_000;

  for (const item of items) {
    const created = new Date(item.created_at).getTime();
    if (created >= startOfToday) {
      groups[0]?.items.push(item);
    } else if (created >= startOfYesterday) {
      groups[1]?.items.push(item);
    } else if (created >= startOfWeek) {
      groups[2]?.items.push(item);
    } else {
      groups[3]?.items.push(item);
    }
  }
  return groups.filter((g) => g.items.length > 0);
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
