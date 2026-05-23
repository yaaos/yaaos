/**
 * Notifications bell — sidebar row with the lucide Bell icon, an
 * unread-count badge, and a popover anchored to the row.
 *
 * Backed by `useNotificationsPopover()` → GET /api/notifications/popover.
 * The mark-one-read mutation runs on row click; "Mark all read" runs on
 * the popover footer link.
 */

import {
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotificationsPopover,
} from "@core/api";
import { Popover, PopoverContent, PopoverTrigger } from "@shared/components/ui/popover";
import { ago } from "@shared/utils/ago";
import { cn } from "@shared/utils/cn";
import { Bell } from "lucide-react";

interface NotificationsBellProps {
  expanded: boolean;
  className?: string;
}

export function NotificationsBell({ expanded, className }: NotificationsBellProps) {
  const { data } = useNotificationsPopover();
  const markOne = useMarkNotificationRead();
  const markAll = useMarkAllNotificationsRead();
  const unreadCount = data?.unread_count ?? 0;
  const items = data?.items ?? [];
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="notifications-bell"
          aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} unread` : "Notifications"}
          className={cn(
            "flex items-center gap-2.5 w-full px-2 py-1.5 rounded text-[12.5px]",
            "text-foreground hover:bg-accent hover:text-accent-foreground transition-colors",
            !expanded && "justify-center",
            className,
          )}
          title={expanded ? undefined : "Notifications"}
        >
          <span className="relative shrink-0">
            <Bell className="w-4 h-4" />
            {unreadCount > 0 && (
              <span className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-1 rounded-full bg-primary text-primary-foreground text-[9px] font-semibold flex items-center justify-center">
                {unreadCount > 99 ? "99+" : unreadCount}
              </span>
            )}
          </span>
          {expanded && (
            <>
              <span className="flex-1 text-left">Notifications</span>
              {unreadCount > 0 && (
                <span className="text-xs text-muted-foreground">{unreadCount}</span>
              )}
            </>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" side="right" sideOffset={8} className="w-[340px] p-2">
        <div className="flex items-center justify-between px-1 pb-2 border-b border-border">
          <h3 className="text-sm font-medium">Notifications</h3>
          <a
            href="/notifications"
            className="text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            See all
          </a>
        </div>
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground py-6 text-center">You're all caught up.</p>
        ) : (
          <>
            <ul className="max-h-[360px] overflow-y-auto py-1">
              {items.map((n) => (
                <li key={n.id}>
                  <button
                    type="button"
                    onClick={() => markOne.mutate(n.id)}
                    data-testid={`notif-${n.id}`}
                    className={cn(
                      "flex flex-col items-start gap-0.5 w-full px-2 py-2 rounded text-left text-xs",
                      "hover:bg-accent hover:text-accent-foreground transition-colors",
                      !n.read_at && "font-medium",
                    )}
                  >
                    <div className="flex w-full items-baseline gap-2">
                      <span className="flex-1 truncate">{n.title}</span>
                      <span className="text-[10.5px] text-muted-foreground shrink-0">
                        {ago(n.created_at)}
                      </span>
                    </div>
                    <span className="text-muted-foreground line-clamp-2">{n.body}</span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="border-t border-border pt-1">
              <button
                type="button"
                onClick={() => markAll.mutate()}
                disabled={markAll.isPending || unreadCount === 0}
                className="w-full px-2 py-1.5 rounded text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors disabled:opacity-50"
              >
                {unreadCount === 0 ? "All read" : "Mark all read"}
              </button>
            </div>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
