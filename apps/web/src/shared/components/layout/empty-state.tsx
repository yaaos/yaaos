/**
 * Empty state — used when a list, panel, or chart has zero rows.
 *
 * Spec from requirements.md § C2:
 * - Centered within the container.
 * - Icon ~64px, muted.
 * - Headline — one short sentence.
 * - Body — one line explaining why / what to do.
 * - Primary action (optional).
 */

import { cn } from "@shared/utils";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

interface EmptyStateProps {
  icon?: LucideIcon;
  headline: string;
  body?: string;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon, headline, body, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn("flex flex-col items-center justify-center text-center py-12 px-4", className)}
    >
      {Icon && (
        <div className="text-muted-foreground mb-3" aria-hidden="true">
          <Icon className="w-12 h-12" />
        </div>
      )}
      <h2 className="text-base font-medium">{headline}</h2>
      {body && <p className="text-sm text-muted-foreground mt-1 max-w-sm">{body}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
