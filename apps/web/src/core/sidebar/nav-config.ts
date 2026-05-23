/**
 * Typed sidebar nav config. Architecture: `{kind: "link" | "group", ...}`.
 *
 * `link` items navigate; `group` items expand to reveal sub-items. Sub-items
 * are always links — single nesting depth.
 *
 * Role gate: a `role: "admin"` entry is hidden when the current membership
 * is "builder". Owner sees everything (Owner > Admin > Builder).
 */

import type { LucideIcon } from "lucide-react";

export type NavRole = "builder" | "admin";

export interface NavLink {
  kind: "link";
  id: string; // stable id used for active-route matching + group keys
  label: string;
  icon: LucideIcon;
  /** Path WITHIN an org context (e.g. "/dashboard"). The sidebar prepends `/orgs/{slug}`. */
  path: string;
  role?: NavRole;
}

export interface NavGroup {
  kind: "group";
  id: string;
  label: string;
  icon: LucideIcon;
  role?: NavRole;
  children: NavLink[];
}

export type NavItem = NavLink | NavGroup;

/**
 * The top-level sidebar nav. Order is canonical — `requirements.md § Top-level nav`.
 *
 * Concrete icons + paths are injected by the consumer (sidebar.tsx) so this
 * file stays free of React/icon imports and is trivial to unit-test.
 */
export interface NavConfig {
  org: NavItem[];
  user: NavLink[]; // popover from the bottom UserCard
}
