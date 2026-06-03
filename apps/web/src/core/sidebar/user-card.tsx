import { useCurrentOrgSlug, useCurrentUser, useLogout } from "@core/api";
import { cn } from "@shared/utils";
import { Link } from "@tanstack/react-router";
import { ChevronUp, Lock, LogOut, Moon, Sun, User as UserIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toggleTheme } from "../layout/theme";

/**
 * Bottom-of-sidebar user card. Shows the cookie-bearer's display name + their
 * `@handle` for the current org. Click opens a popover with `User > Details`,
 * `User > Security`, theme toggle, and `Log off` — the canonical User
 * section nav, now also hosting global chrome (theme) since the topbar was
 * removed.
 */
export function UserCard({ expanded }: { expanded: boolean }) {
  const { data } = useCurrentUser();
  const slug = useCurrentOrgSlug();
  const logout = useLogout();
  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">(() =>
    typeof document !== "undefined" &&
    document.documentElement.getAttribute("data-theme") === "light"
      ? "light"
      : "dark",
  );
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
  const currentOrg = slug ? data.memberships.find((m) => m.slug === slug) : null;
  const initials = (data.user.display_name || data.user.primary_email || "?")
    .split(/\s+/)
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();

  const onLogoff = () => {
    // Single-session sign-out; "Sign out of all sessions" lives on the
    // Security page for the all-devices nuke.
    logout.mutate(undefined, {
      onSettled: () => {
        window.location.href = "/login";
      },
    });
  };

  const onToggleTheme = () => {
    setTheme(toggleTheme());
  };

  return (
    <div ref={ref} className="relative border-t border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="user-card-button"
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left text-foreground hover:bg-accent hover:text-foreground",
          !expanded && "justify-center",
        )}
        title={expanded ? undefined : data.user.display_name || ""}
      >
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-accent text-[10.5px] font-semibold text-foreground">
          {initials}
        </span>
        {expanded && (
          <div className="flex min-w-0 flex-col leading-tight">
            <span className="truncate text-[12.5px] font-medium">
              {data.user.display_name || data.user.primary_email}
            </span>
            {currentOrg && (
              <span className="truncate font-mono text-[10.5px] text-muted-foreground">
                @{currentOrg.handle}
              </span>
            )}
          </div>
        )}
        {expanded && <ChevronUp className="ml-auto h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
      </button>
      {open && (
        <div
          data-testid="user-card-popover"
          className="absolute bottom-full left-2 right-2 mb-1 rounded border border-border bg-card py-1 shadow-lg"
        >
          {slug && (
            <>
              <Link
                to="/orgs/$slug/user/details"
                params={{ slug }}
                className="flex items-center gap-2 px-3 py-1.5 text-[12.5px] text-foreground hover:bg-accent hover:text-foreground"
                data-testid="user-nav-details"
              >
                <UserIcon className="h-3.5 w-3.5" /> Details
              </Link>
              <Link
                to="/orgs/$slug/user/security"
                params={{ slug }}
                className="flex items-center gap-2 px-3 py-1.5 text-[12.5px] text-foreground hover:bg-accent hover:text-foreground"
                data-testid="user-nav-security"
              >
                <Lock className="h-3.5 w-3.5" /> Security
              </Link>
            </>
          )}
          <button
            type="button"
            onClick={onToggleTheme}
            data-testid="user-nav-theme"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-foreground hover:bg-accent hover:text-foreground"
          >
            {theme === "dark" ? (
              <>
                <Sun className="h-3.5 w-3.5" /> Light theme
              </>
            ) : (
              <>
                <Moon className="h-3.5 w-3.5" /> Dark theme
              </>
            )}
          </button>
          <button
            type="button"
            onClick={onLogoff}
            data-testid="user-nav-logoff"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-foreground hover:bg-accent hover:text-foreground"
          >
            <LogOut className="h-3.5 w-3.5" /> Log off
          </button>
        </div>
      )}
    </div>
  );
}
