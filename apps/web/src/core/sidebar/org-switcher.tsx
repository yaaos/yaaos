/**
 * Org switcher — sidebar chip showing the current org with a dropdown of
 * the user's other orgs + a "View all orgs" link to /orgs.
 *
 * Data source: `useMyOrgs()` → GET /api/orgs/mine.
 * Per B3: lives at the top of the sidebar, above the org-scoped nav block.
 */

import { useCurrentOrgSlug } from "@core/api/public/org-context";
import { useMyOrgs } from "@core/api/public/queries";
import { Popover, PopoverContent, PopoverTrigger } from "@shared/components/ui/popover";
import { cn } from "@shared/utils/public/cn";
import { Link } from "@tanstack/react-router";
import { Building2, Check, ChevronsUpDown } from "lucide-react";
import { useState } from "react";

interface OrgSwitcherProps {
  expanded: boolean;
  className?: string;
}

export function OrgSwitcher({ expanded, className }: OrgSwitcherProps) {
  const { data: orgs } = useMyOrgs();
  const [open, setOpen] = useState(false);

  const currentSlug = useCurrentOrgSlug();
  const currentOrg = orgs?.find((o) => o.slug === currentSlug);

  const onPick = (slug: string) => {
    setOpen(false);
    if (slug === currentSlug) return;
    window.location.href = `/org/${slug}/workspaces`;
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="org-switcher-chip"
          className={cn(
            "flex items-center gap-2 px-2 py-1.5 rounded text-[12.5px] transition-colors w-full",
            "text-foreground hover:bg-accent hover:text-accent-foreground",
            !expanded && "justify-center",
            className,
          )}
          title={expanded ? undefined : (currentOrg?.name ?? "Switch organization")}
        >
          <Building2 className="w-4 h-4 shrink-0" />
          {expanded && (
            <>
              <span className="flex-1 truncate text-left font-medium">
                {currentOrg?.name ?? currentOrg?.slug ?? "Pick an org"}
              </span>
              <ChevronsUpDown className="w-3.5 h-3.5 shrink-0 text-muted-foreground" />
            </>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" sideOffset={4} className="w-[240px] p-1">
        {orgs && orgs.length > 0 ? (
          <>
            {orgs.map((o) => (
              <button
                key={o.slug}
                type="button"
                onClick={() => onPick(o.slug)}
                data-testid={`org-switcher-option-${o.slug}`}
                className={cn(
                  "flex items-center gap-2 w-full px-2 py-1.5 rounded text-[12.5px] text-left",
                  "hover:bg-accent hover:text-accent-foreground transition-colors",
                )}
              >
                <span className="flex-1 truncate">
                  <span className="font-medium">{o.name || o.slug}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{o.role}</span>
                </span>
                {o.slug === currentSlug && <Check className="w-3.5 h-3.5 shrink-0 text-primary" />}
              </button>
            ))}
            <div className="border-t border-border my-1" />
          </>
        ) : (
          <p className="px-2 py-2 text-xs text-muted-foreground">No organizations yet.</p>
        )}
        <Link
          to="/orgs"
          onClick={() => setOpen(false)}
          className="block px-2 py-1.5 rounded text-[12.5px] text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors"
        >
          View all organizations
        </Link>
      </PopoverContent>
    </Popover>
  );
}
