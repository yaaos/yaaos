/**
 * Notifications bell — sidebar row with the lucide Bell icon, an
 * unread-count badge, and a popover anchored to the row.
 *
 * Phase 2 ships the shell only: the popover renders a placeholder
 * empty-state until Phase 7 wires the real `apps/backend/app/domain/
 * notifications` module and `useNotificationsPopover()` data.
 */

import { Popover, PopoverContent, PopoverTrigger } from "@shared/components/ui/popover";
import { cn } from "@shared/utils/cn";
import { Bell } from "lucide-react";

interface NotificationsBellProps {
  expanded: boolean;
  /** Optional unread count — Phase 2 always passes 0 (no backend). */
  unreadCount?: number;
  className?: string;
}

export function NotificationsBell({
  expanded,
  unreadCount = 0,
  className,
}: NotificationsBellProps) {
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
      <PopoverContent align="start" side="right" sideOffset={8} className="w-[320px] p-3">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-medium">Notifications</h3>
          <a
            href="/notifications"
            className="text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            See all
          </a>
        </div>
        <p className="text-xs text-muted-foreground py-4 text-center">You're all caught up.</p>
      </PopoverContent>
    </Popover>
  );
}
