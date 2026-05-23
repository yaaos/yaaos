import { useCurrentUser, useLogoutAll } from "@domain/auth";
import { cn } from "@shared/utils/cn";
import { ChevronUp, Lock, LogOut, User as UserIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

/**
 * Bottom-of-sidebar user card. Shows the cookie-bearer's display name + their
 * `@handle` for the current org. Click opens a popover with `User > Details`,
 * `User > Security`, and `Log off` — the canonical M03 User section nav.
 */
export function UserCard({ expanded }: { expanded: boolean }) {
  const { data } = useCurrentUser();
  const logoutAll = useLogoutAll();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  if (!data) return null;
  const currentOrg = data.orgs.find((o) => o.slug === data.current_org_slug);
  const initials = (data.user.display_name || data.user.primary_email || "?")
    .split(/\s+/)
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();

  const onLogoff = () => {
    logoutAll.mutate(undefined, {
      onSettled: () => {
        window.location.href = "/login";
      },
    });
  };

  return (
    <div ref={ref} className="relative border-t border-border-soft">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="user-card-button"
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left text-text-2 hover:bg-hover hover:text-text",
          !expanded && "justify-center",
        )}
        title={expanded ? undefined : data.user.display_name || ""}
      >
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-accent-bg text-[10.5px] font-semibold text-text">
          {initials}
        </span>
        {expanded && (
          <div className="flex min-w-0 flex-col leading-tight">
            <span className="truncate text-[12.5px] font-medium">
              {data.user.display_name || data.user.primary_email}
            </span>
            {currentOrg && (
              <span className="truncate font-mono text-[10.5px] text-text-4">
                @{currentOrg.handle}
              </span>
            )}
          </div>
        )}
        {expanded && <ChevronUp className="ml-auto h-3.5 w-3.5 shrink-0 text-text-4" />}
      </button>
      {open && (
        <div
          data-testid="user-card-popover"
          className="absolute bottom-full left-2 right-2 mb-1 rounded border border-border-soft bg-bg-2 py-1 shadow-lg"
        >
          <a
            href="/user/details"
            className="flex items-center gap-2 px-3 py-1.5 text-[12.5px] text-text-2 hover:bg-hover hover:text-text"
            data-testid="user-nav-details"
          >
            <UserIcon className="h-3.5 w-3.5" /> Details
          </a>
          <a
            href="/user/security"
            className="flex items-center gap-2 px-3 py-1.5 text-[12.5px] text-text-2 hover:bg-hover hover:text-text"
            data-testid="user-nav-security"
          >
            <Lock className="h-3.5 w-3.5" /> Security
          </a>
          <button
            type="button"
            onClick={onLogoff}
            data-testid="user-nav-logoff"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-text-2 hover:bg-hover hover:text-text"
          >
            <LogOut className="h-3.5 w-3.5" /> Log off
          </button>
        </div>
      )}
    </div>
  );
}
