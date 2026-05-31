import { useCurrentOrgSlug } from "@core/api";
import { useCurrentUser } from "@domain/auth";
import { NotificationsBell, OrgSwitcher } from "@shared/components/chrome";
import { Popover, PopoverContent, PopoverTrigger } from "@shared/components/ui/popover";
import { cn } from "@shared/utils/cn";
import { Link, useRouterState } from "@tanstack/react-router";
import {
  Brain,
  ChevronRight,
  KeyRound,
  LayoutDashboard,
  ListChecks,
  Pin,
  PinOff,
  Settings,
  ShieldCheck,
  Ticket,
  Users,
  Workflow,
} from "lucide-react";
import { useEffect, useState } from "react";
import { getSidebarPinned, setSidebarPinned } from "../layout/theme";
import type { NavConfig, NavGroup, NavItem, NavLink, NavRole } from "./nav-config";
import { useCollapseState } from "./use-collapse-state";
import { UserCard } from "./user-card";

const NAV: NavConfig = {
  org: [
    {
      kind: "link",
      id: "dashboard",
      label: "Dashboard",
      icon: LayoutDashboard,
      path: "/dashboard",
    },
    { kind: "link", id: "tickets", label: "Tickets", icon: Ticket, path: "/tickets" },
    { kind: "link", id: "lessons", label: "Lessons", icon: Brain, path: "/lessons" },
    {
      kind: "group",
      id: "org-settings",
      label: "Org Settings",
      icon: Settings,
      // Group itself has no role gate — Members must see the Members sub-item
      // even when every other sub-item is hidden. Per-child gates do the work.
      children: [
        {
          kind: "link",
          id: "auth",
          label: "Auth",
          icon: ShieldCheck,
          path: "/settings/auth",
          role: "admin",
        },
        { kind: "link", id: "members", label: "Members", icon: Users, path: "/settings/members" },
        {
          kind: "link",
          id: "vcs",
          label: "VCS",
          icon: Workflow,
          path: "/settings/vcs",
          role: "admin",
        },
        {
          kind: "link",
          id: "coding-agents",
          label: "Coding Agents",
          icon: ListChecks,
          path: "/settings/coding-agents",
          role: "admin",
        },
        {
          kind: "link",
          id: "api-keys",
          label: "API Keys",
          icon: KeyRound,
          path: "/settings/api-keys",
          role: "admin",
        },
        {
          kind: "link",
          id: "audit",
          label: "Audit",
          icon: ListChecks,
          path: "/settings/audit",
          role: "admin",
        },
        {
          kind: "link",
          id: "workspaces",
          label: "Workspaces",
          icon: Workflow,
          path: "/settings/workspaces",
          role: "admin",
        },
      ],
    },
  ],
  user: [],
};

/** True iff the user's role in the current org satisfies the gate. */
function _roleCovers(currentRole: NavRole | undefined, required: NavRole | undefined): boolean {
  if (!required) return true;
  if (!currentRole) return false;
  const order: Record<NavRole, number> = { builder: 0, admin: 1 };
  return order[currentRole] >= order[required];
}

export function Sidebar() {
  const [pinned, setPinned] = useState<boolean>(() => getSidebarPinned());
  const { location } = useRouterState();
  const active = location.pathname;
  const slug = useCurrentOrgSlug();
  const { data: user } = useCurrentUser();
  const { isCollapsed, toggle, setCollapsed } = useCollapseState();

  // Auto-collapse rule: a group stays open only while one of its children
  // is the active route. Leave the section → it collapses. User can still
  // manually expand from anywhere; the next navigation re-applies the rule.
  useEffect(() => {
    for (const item of NAV.org) {
      if (item.kind !== "group") continue;
      const anyActive = item.children.some((c) =>
        active.startsWith(slug ? `/orgs/${slug}${c.path}` : c.path),
      );
      if (!anyActive) setCollapsed(item.id, true);
    }
  }, [active, slug, setCollapsed]);

  const currentMembership = slug ? user?.memberships.find((m) => m.slug === slug) : undefined;
  // Owner satisfies any admin-gated nav item (Owner > Admin > Builder).
  const effectiveRole: NavRole | undefined =
    currentMembership?.role === "owner" || currentMembership?.role === "admin"
      ? "admin"
      : currentMembership?.role === "builder"
        ? "builder"
        : undefined;

  const togglePin = () => {
    const next = !pinned;
    setPinned(next);
    setSidebarPinned(next);
  };

  const isItemVisible = (item: NavItem) => _roleCovers(effectiveRole, item.role);

  // Org-scoped path or naked path when no org context — keeps legacy
  // routes (`/dashboard` etc.) working.
  const absolutePath = (relativePath: string) =>
    slug ? `/orgs/${slug}${relativePath}` : relativePath;

  return (
    <aside
      className={cn(
        "flex flex-col bg-card border-r border-border shrink-0",
        pinned ? "w-[220px]" : "w-[56px]",
      )}
      data-testid="sidebar"
      data-pinned={pinned}
    >
      <div
        className={cn(
          "flex items-center border-b border-border",
          // Pinned: lockup spans full sidebar width edge-to-edge; height
          // grows proportionally with the lockup's natural ~3:1 ratio.
          // Collapsed: mark centered in the 56px rail.
          pinned ? "px-3 py-3" : "justify-center h-[56px]",
        )}
      >
        <Link to="/" className={cn("block", pinned && "w-full")} aria-label="yaaos home">
          {pinned ? (
            <>
              <img
                src="/logos/yaaos-lockup-dark.svg"
                alt="yaaos"
                className="w-full dark:block hidden"
              />
              <img
                src="/logos/yaaos-lockup-light.svg"
                alt="yaaos"
                className="w-full dark:hidden block"
              />
            </>
          ) : (
            <>
              <img
                src="/logos/yaaos-mark-dark.svg"
                alt="yaaos"
                className="h-7 w-7 dark:block hidden"
              />
              <img
                src="/logos/yaaos-mark-light.svg"
                alt="yaaos"
                className="h-7 w-7 dark:hidden block"
              />
            </>
          )}
        </Link>
      </div>

      {/* Org switcher chip — defines the current org context. */}
      <div className="px-1.5 py-2 border-b border-border">
        <OrgSwitcher expanded={pinned} />
      </div>

      <nav className="flex flex-col gap-0.5 px-1.5 py-2 flex-1 overflow-y-auto">
        {NAV.org.filter(isItemVisible).map((item) => {
          if (item.kind === "link") return renderLink(item, { active, pinned, absolutePath });
          // Apply role gating per child too. Hide the group entirely when no
          // child survives the filter.
          const visibleChildren = item.children.filter(isItemVisible);
          if (visibleChildren.length === 0) return null;
          return renderGroup(
            { ...item, children: visibleChildren },
            {
              active,
              pinned,
              absolutePath,
              collapsed: isCollapsed(item.id),
              onToggle: () => toggle(item.id),
            },
          );
        })}
      </nav>

      {/* User-scoped zone — cross-org. */}
      <div className="px-1.5 py-2 border-t border-border">
        <NotificationsBell expanded={pinned} />
      </div>

      <UserCard expanded={pinned} />

      <div className="flex items-center gap-2 px-3 py-2 border-t border-border">
        {pinned && (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-success" />
            <span className="mono text-muted-foreground text-[10.5px] flex-1">v0.0.1</span>
          </>
        )}
        <button
          type="button"
          onClick={togglePin}
          data-testid="sidebar-pin"
          className={cn(
            "rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground",
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

interface RenderContext {
  active: string;
  pinned: boolean;
  absolutePath: (relative: string) => string;
}

function renderLink(item: NavLink, ctx: RenderContext, depth: 0 | 1 = 0) {
  const Icon = item.icon;
  const href = ctx.absolutePath(item.path);
  const isActive = ctx.active.startsWith(href);
  // Active state is ONLY a background color change — no border, no margin shift,
  // so the item stays in the exact same position whether selected or not.
  return (
    <Link
      key={item.id}
      to={href}
      data-testid={`nav-${item.id}`}
      data-active={isActive || undefined}
      className={cn(
        "flex items-center gap-2.5 px-2 py-1.5 rounded text-[12.5px] transition-colors",
        !ctx.pinned && "justify-center",
        depth === 1 && "ml-5",
        isActive
          ? "bg-accent text-foreground"
          : "text-foreground hover:bg-accent hover:text-foreground",
      )}
      title={ctx.pinned ? undefined : item.label}
    >
      <Icon className="w-4 h-4 shrink-0" />
      {ctx.pinned && <span>{item.label}</span>}
    </Link>
  );
}

function renderGroup(
  item: NavGroup,
  ctx: RenderContext & { collapsed: boolean; onToggle: () => void },
) {
  const Icon = item.icon;
  const hasActiveChild = item.children.some((c) => ctx.active.startsWith(ctx.absolutePath(c.path)));

  // Rail mode: the group's children can't render inline (no room for labels),
  // so the icon opens a popover anchored to the right with the full sub-menu.
  if (!ctx.pinned) {
    return (
      <Popover key={item.id}>
        <PopoverTrigger asChild>
          <button
            type="button"
            data-testid={`nav-group-${item.id}`}
            data-active={hasActiveChild || undefined}
            className={cn(
              "flex w-full items-center justify-center px-2 py-1.5 rounded text-[12.5px] transition-colors",
              hasActiveChild
                ? "text-foreground bg-accent"
                : "text-foreground hover:bg-accent hover:text-foreground",
            )}
            title={item.label}
          >
            <Icon className="w-4 h-4 shrink-0" />
          </button>
        </PopoverTrigger>
        <PopoverContent side="right" align="start" sideOffset={8} className="w-48 p-1">
          <div className="px-2 py-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
            {item.label}
          </div>
          <div className="flex flex-col gap-0.5">
            {item.children.map((c) => {
              const ChildIcon = c.icon;
              const href = ctx.absolutePath(c.path);
              const isActive = ctx.active.startsWith(href);
              return (
                <Link
                  key={c.id}
                  to={href}
                  data-testid={`nav-${c.id}`}
                  data-active={isActive || undefined}
                  className={cn(
                    "flex items-center gap-2.5 px-2 py-1.5 rounded text-[12.5px] transition-colors",
                    isActive
                      ? "bg-accent text-foreground"
                      : "text-foreground hover:bg-accent hover:text-foreground",
                  )}
                >
                  <ChildIcon className="w-4 h-4 shrink-0" />
                  <span>{c.label}</span>
                </Link>
              );
            })}
          </div>
        </PopoverContent>
      </Popover>
    );
  }

  return (
    <div key={item.id}>
      <button
        type="button"
        onClick={ctx.onToggle}
        data-testid={`nav-group-${item.id}`}
        data-active={hasActiveChild || undefined}
        data-collapsed={ctx.collapsed || undefined}
        className={cn(
          "flex w-full items-center gap-2.5 px-2 py-1.5 rounded text-[12.5px] transition-colors",
          hasActiveChild
            ? "text-foreground bg-accent"
            : "text-foreground hover:bg-accent hover:text-foreground",
        )}
      >
        <Icon className="w-4 h-4 shrink-0" />
        <span className="flex-1 text-left">{item.label}</span>
        <ChevronRight
          className={cn(
            "w-3.5 h-3.5 shrink-0 text-muted-foreground transition-transform",
            !ctx.collapsed && "rotate-90",
          )}
        />
      </button>
      {!ctx.collapsed && (
        <div className="flex flex-col gap-0.5 mt-0.5">
          {item.children.map((c) => renderLink(c, ctx, 1))}
        </div>
      )}
    </div>
  );
}
