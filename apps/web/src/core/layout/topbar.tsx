import { useCurrentUser, useLogout } from "@domain/auth";
import { Link } from "@tanstack/react-router";
import { LogOut, Moon, Sun, User } from "lucide-react";
import { useState } from "react";
import { toggleTheme } from "./theme";

export function Topbar({ crumb }: { crumb: string }) {
  const [theme, setTheme] = useState<"light" | "dark">(() =>
    document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark",
  );
  const { data: user } = useCurrentUser();
  const logout = useLogout();

  const onToggle = () => {
    const next = toggleTheme();
    setTheme(next);
  };

  const onLogout = () => {
    logout.mutate(undefined, {
      // Hard navigation so the SPA tears down all in-memory query caches.
      onSettled: () => {
        window.location.href = "/login";
      },
    });
  };

  return (
    <header className="flex items-center gap-3 h-[44px] border-b border-border bg-card px-4 shrink-0">
      <div className="mono text-foreground text-[12px]">{crumb}</div>
      <div className="flex-1" />
      <button
        type="button"
        onClick={onToggle}
        className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
        title={theme === "dark" ? "Switch to light" : "Switch to dark"}
      >
        {theme === "dark" ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
      </button>
      <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-success text-success-foreground text-[10.5px] font-medium">
        <span className="w-1.5 h-1.5 rounded-full bg-success-foreground animate-pulse" />
        live
      </span>
      {user && (
        <>
          <Link
            to="/user/details"
            className="flex items-center gap-1.5 rounded px-2 py-1 text-muted-foreground hover:bg-accent hover:text-foreground text-[12px]"
            title="Account"
            data-testid="account-link"
          >
            <User className="w-3.5 h-3.5" />
            <span className="hidden md:inline">
              {user.user.display_name || user.user.primary_email}
            </span>
          </Link>
          <button
            type="button"
            onClick={onLogout}
            disabled={logout.isPending}
            className="flex items-center gap-1.5 rounded px-2 py-1 text-muted-foreground hover:bg-accent hover:text-foreground text-[12px] disabled:opacity-50"
            title="Sign out"
            data-testid="logout"
          >
            <LogOut className="w-3.5 h-3.5" />
            <span className="hidden md:inline">Sign out</span>
          </button>
        </>
      )}
    </header>
  );
}
