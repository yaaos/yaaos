/**
 * "Not configured" gate banner.
 *
 * Renders a non-intrusive banner on every org-scoped surface when the org
 * is missing a prerequisite (VCS plugin, ≥1 coding agent, valid API key,
 * or workspace agent IAM ARN). Builders see "ask your admin"; Admins see a
 * direct link to settings. Dashboard's "setup required" state subsumes
 * the banner there; everywhere else this is the affordance.
 *
 * Data source: `useConfigStatus()` → `/api/orgs/config-status`.
 */

import { useConfigStatus, useCurrentOrgSlug } from "@core/api";
import { useCurrentUser } from "@domain/auth";
import { cn } from "@shared/utils/cn";
import { AlertTriangle } from "lucide-react";

const HUMAN_LABEL: Record<string, string> = {
  vcs: "Connect a VCS provider",
  coding_agent: "Configure a coding agent",
  api_key: "Add an API key",
  workspace: "Configure a workspace agent (IAM ARN + region)",
};

interface NotConfiguredBannerProps {
  className?: string;
}

export function NotConfiguredBanner({ className }: NotConfiguredBannerProps) {
  const { data: status } = useConfigStatus();
  const { data: user } = useCurrentUser();
  const slug = useCurrentOrgSlug();
  if (!status || status.configured) return null;

  const currentMembership = slug ? user?.memberships.find((m) => m.slug === slug) : undefined;
  const isAdminOrOwner = currentMembership?.role === "admin" || currentMembership?.role === "owner";
  const missingLabels = status.missing.map((m) => HUMAN_LABEL[m] ?? m).join(", ");
  const adminLine =
    status.admins.length > 0
      ? `Ask ${status.admins.map((a) => a.display_name).join(", ")} to finish setup.`
      : "Ask an admin to finish setup.";

  return (
    <output
      className={cn(
        "flex items-start gap-3 px-3 py-2 rounded-md border border-warning/40 bg-warning/10",
        className,
      )}
    >
      <AlertTriangle className="w-4 h-4 text-warning shrink-0 mt-0.5" aria-hidden="true" />
      <div className="flex-1 min-w-0 text-sm">
        <span className="font-medium">yaaos is not fully configured.</span>{" "}
        {isAdminOrOwner ? (
          <span className="text-muted-foreground">Still to do: {missingLabels}.</span>
        ) : (
          <span className="text-muted-foreground">{adminLine}</span>
        )}
      </div>
    </output>
  );
}
