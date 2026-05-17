import { cn } from "@shared/utils/cn";
import { Link, useRouterState } from "@tanstack/react-router";
import { Brain, LayoutDashboard, Pin, PinOff, Settings, Ticket } from "lucide-react";
import { useState } from "react";
import { getSidebarPinned, setSidebarPinned } from "./theme";

const NAV = [
  { path: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { path: "/tickets", label: "Tickets", icon: Ticket },
  { path: "/memory", label: "Memory", icon: Brain },
  { path: "/settings", label: "Settings", icon: Settings },
] as const;

export function Sidebar() {
  const [pinned, setPinned] = useState<boolean>(() => getSidebarPinned());
  const { location } = useRouterState();
  const active = location.pathname;

  const togglePin = () => {
    const next = !pinned;
    setPinned(next);
    setSidebarPinned(next);
  };

  return (
    <aside
      className={cn(
        "flex flex-col bg-bg-2 border-r border-border-soft shrink-0",
        pinned ? "w-[220px]" : "w-[48px]",
      )}
    >
      {/* Logo placeholder — real logo lands later. Slot is reserved. */}
      <div className="flex items-center gap-2 px-3 py-3 border-b border-border-soft h-[56px]">
        <div className="w-6 h-6 rounded bg-gradient-to-br from-accent to-accent-2 flex items-center justify-center text-white font-mono font-bold text-[12px]">
          Y
        </div>
        {pinned && (
          <div className="flex flex-col leading-tight">
            <span className="font-semibold">yaaos</span>
            <span className="mono text-text-4 text-[9px] uppercase tracking-wider">
              logo · placeholder
            </span>
          </div>
        )}
      </div>

      <nav className="flex flex-col gap-0.5 px-1.5 py-2 flex-1">
        {pinned && (
          <div className="mono text-text-4 text-[9.5px] uppercase tracking-wider px-2 py-1.5">
            Workspace
          </div>
        )}
        {NAV.map((item) => {
          const Icon = item.icon;
          const isActive = active.startsWith(item.path);
          return (
            <Link
              key={item.path}
              to={item.path}
              className={cn(
                "flex items-center gap-2.5 px-2 py-1.5 rounded text-[12.5px] transition-colors",
                isActive
                  ? "bg-accent-bg text-text border-l-2 border-accent -ml-[2px] pl-[10px]"
                  : "text-text-2 hover:bg-hover hover:text-text",
              )}
              title={pinned ? undefined : item.label}
            >
              <Icon className="w-4 h-4 shrink-0" />
              {pinned && <span>{item.label}</span>}
            </Link>
          );
        })}
      </nav>

      <div className="flex items-center gap-2 px-3 py-2 border-t border-border-soft">
        {pinned && (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-success" />
            <span className="mono text-text-4 text-[10.5px] flex-1">v0.0.1</span>
          </>
        )}
        <button
          type="button"
          onClick={togglePin}
          className={cn(
            "rounded p-1 text-text-3 hover:bg-hover hover:text-text",
            !pinned && "mx-auto",
          )}
          title={pinned ? "Unpin (collapse to rail)" : "Pin (always show)"}
        >
          {pinned ? <Pin className="w-3.5 h-3.5" /> : <PinOff className="w-3.5 h-3.5" />}
        </button>
      </div>
    </aside>
  );
}
