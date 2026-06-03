/**
 * Page header — title + optional subtitle + actions slot.
 *
 * Every surface composes this at the top of its content area. Title is
 * h1; subtitle is muted text below; `actions` slot is right-aligned for
 * primary affordances (Save, Add, …).
 */

import { cn } from "@shared/utils";
import type { ReactNode } from "react";

interface PageHeaderProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  className?: string;
}

export function PageHeader({ title, subtitle, actions, className }: PageHeaderProps) {
  return (
    <header className={cn("flex items-start justify-between gap-4 mb-6", className)}>
      <div className="flex-1 min-w-0">
        <h1 className="text-2xl font-semibold leading-tight">{title}</h1>
        {subtitle && <p className="text-muted-foreground text-sm mt-1">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </header>
  );
}
