import type React from "react";

/**
 * Org Settings shell.
 *
 * Passthrough wrapper — the SPA has no top bar, so settings pages render
 * their content as the topmost element. The sidebar's Org Settings group
 * already shows which sub-page is active; a horizontal tab strip would
 * duplicate that nav and violate the "no topbar ever" rule (see
 * apps/web/docs/design.md § Layout).
 *
 * Kept as a component (rather than removed entirely) so per-tab role gating
 * and any future shared chrome land in one place. The `active` prop is now
 * informational only — preserved for callers but unused.
 *
 * Lives in `shared/` (not `domain/org_settings/`) because a second domain
 * module (`domain/pipeline_settings`) needs the same shell — cross-domain
 * imports are forbidden, so this graduated per the rule-of-three in
 * apps/web/docs/components.md.
 */
export function OrgSettingsLayout({
  active: _active,
  children,
}: {
  active: string;
  children: React.ReactNode;
}) {
  return <div className="flex h-full flex-col">{children}</div>;
}
