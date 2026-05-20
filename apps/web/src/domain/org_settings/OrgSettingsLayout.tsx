import { getCurrentOrgSlug } from "@core/api";
import { useCurrentUser } from "@domain/auth";
import { cn } from "@shared/utils/cn";
import type React from "react";

/**
 * Org Settings shell. Renders the active sub-page inside a tab-style header.
 * Per-tab role gating mirrors the sidebar: Members is the only tab a
 * non-admin can hit; the rest are Owner+Admin only.
 */
interface SettingsTab {
  id: string;
  label: string;
  path: string;
  role?: "member" | "admin";
}

const TABS: SettingsTab[] = [
  { id: "auth", label: "Auth", path: "/settings/auth", role: "admin" },
  { id: "members", label: "Members", path: "/settings/members" },
  { id: "vcs", label: "VCS", path: "/settings/vcs", role: "admin" },
  { id: "coding-agents", label: "Coding Agents", path: "/settings/coding-agents", role: "admin" },
  { id: "byok", label: "BYOK", path: "/settings/byok", role: "admin" },
  { id: "integrations", label: "Integrations", path: "/settings/integrations", role: "admin" },
  { id: "audit", label: "Audit", path: "/settings/audit", role: "admin" },
];

export function OrgSettingsLayout({
  active,
  children,
}: {
  active: string;
  children: React.ReactNode;
}) {
  const slug = getCurrentOrgSlug();
  const { data: user } = useCurrentUser();
  const membership = user?.orgs.find((o) => o.slug === user?.current_org_slug);
  const isAdmin = membership?.role === "owner" || membership?.role === "admin";
  const visibleTabs = TABS.filter((t) => !t.role || (t.role === "admin" && isAdmin));

  const absolutePath = (relative: string) => (slug ? `/orgs/${slug}${relative}` : relative);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-end gap-1 border-b border-border-soft bg-bg-2 px-4 pt-3">
        <h1 className="text-[18px] font-semibold tracking-tight mr-4 mb-2">Org Settings</h1>
        <nav className="flex gap-1" data-testid="org-settings-tabs">
          {visibleTabs.map((t) => {
            const isActive = t.id === active;
            return (
              <a
                key={t.id}
                href={absolutePath(t.path)}
                data-testid={`tab-${t.id}`}
                data-active={isActive || undefined}
                className={cn(
                  "rounded-t border-b-2 px-3 py-1.5 text-[12.5px] transition-colors",
                  isActive
                    ? "border-accent text-text bg-bg"
                    : "border-transparent text-text-3 hover:text-text hover:bg-hover",
                )}
              >
                {t.label}
              </a>
            );
          })}
        </nav>
      </div>
      <div className="flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}
