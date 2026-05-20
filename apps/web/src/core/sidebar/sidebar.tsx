import { getCurrentOrgSlug } from "@core/api";
import { useCurrentUser } from "@domain/auth";
import { cn } from "@shared/utils/cn";
import { useRouterState } from "@tanstack/react-router";
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
import { useState } from "react";
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
    { kind: "link", id: "memory", label: "Memory", icon: Brain, path: "/memory" },
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
          id: "byok",
          label: "BYOK",
          icon: KeyRound,
          path: "/settings/byok",
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
      ],
    },
  ],
  user: [],
};

/** True iff the user's role in the current org satisfies the gate. */
function _roleCovers(currentRole: NavRole | undefined, required: NavRole | undefined): boolean {
  if (!required) return true;
  if (!currentRole) return false;
  const order: Record<NavRole, number> = { member: 0, admin: 1 };
  return order[currentRole] >= order[required];
}

export function Sidebar() {
  const [pinned, setPinned] = useState<boolean>(() => getSidebarPinned());
  const { location } = useRouterState();
  const active = location.pathname;
  const slug = getCurrentOrgSlug();
  const { data: user } = useCurrentUser();
  const { isCollapsed, toggle } = useCollapseState();

  const currentMembership = user?.orgs.find((o) => o.slug === user?.current_org_slug);
  // Owner satisfies any admin-gated nav item (Owner > Admin > Member).
  const effectiveRole: NavRole | undefined =
    currentMembership?.role === "owner" || currentMembership?.role === "admin"
      ? "admin"
      : currentMembership?.role === "member"
        ? "member"
        : undefined;

  const togglePin = () => {
    const next = !pinned;
    setPinned(next);
    setSidebarPinned(next);
  };

  const isItemVisible = (item: NavItem) => _roleCovers(effectiveRole, item.role);

  // Org-scoped path or naked path when no org context — keeps M02 legacy
  // routes (`/dashboard` etc.) working.
  const absolutePath = (relativePath: string) =>
    slug ? `/orgs/${slug}${relativePath}` : relativePath;

  return (
    <aside
      className={cn(
        "flex flex-col bg-bg-2 border-r border-border-soft shrink-0",
        pinned ? "w-[220px]" : "w-[56px]",
      )}
      data-testid="sidebar"
      data-pinned={pinned}
    >
      <div className="flex items-center gap-2 px-3 py-3 border-b border-border-soft h-[56px]">
        <div className="w-6 h-6 rounded bg-gradient-to-br from-accent to-accent-2 flex items-center justify-center text-white font-mono font-bold text-[12px]">
          Y
        </div>
        {pinned && <span className="font-semibold">yaaos</span>}
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

      <UserCard expanded={pinned} />

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
          data-testid="sidebar-pin"
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

interface RenderContext {
  active: string;
  pinned: boolean;
  absolutePath: (relative: string) => string;
}

function renderLink(item: NavLink, ctx: RenderContext, depth: 0 | 1 = 0) {
  const Icon = item.icon;
  const href = ctx.absolutePath(item.path);
  const isActive = ctx.active.startsWith(href);
  return (
    <a
      key={item.id}
      href={href}
      data-testid={`nav-${item.id}`}
      data-active={isActive || undefined}
      className={cn(
        "flex items-center gap-2.5 px-2 py-1.5 rounded text-[12.5px] transition-colors",
        depth === 1 && "ml-5",
        isActive
          ? "bg-accent-bg text-text border-l-2 border-accent -ml-[2px] pl-[10px]"
          : "text-text-2 hover:bg-hover hover:text-text",
      )}
      title={ctx.pinned ? undefined : item.label}
    >
      <Icon className="w-4 h-4 shrink-0" />
      {ctx.pinned && <span>{item.label}</span>}
    </a>
  );
}

function renderGroup(
  item: NavGroup,
  ctx: RenderContext & { collapsed: boolean; onToggle: () => void },
) {
  const Icon = item.icon;
  const hasActiveChild = item.children.some((c) => ctx.active.startsWith(ctx.absolutePath(c.path)));
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
          hasActiveChild ? "text-text bg-accent-bg" : "text-text-2 hover:bg-hover hover:text-text",
        )}
        title={ctx.pinned ? undefined : item.label}
      >
        <Icon className="w-4 h-4 shrink-0" />
        {ctx.pinned && <span className="flex-1 text-left">{item.label}</span>}
        {ctx.pinned && (
          <ChevronRight
            className={cn(
              "w-3.5 h-3.5 shrink-0 text-text-4 transition-transform",
              !ctx.collapsed && "rotate-90",
            )}
          />
        )}
      </button>
      {!ctx.collapsed && ctx.pinned && (
        <div className="flex flex-col gap-0.5 mt-0.5">
          {item.children.map((c) => renderLink(c, ctx, 1))}
        </div>
      )}
    </div>
  );
}
