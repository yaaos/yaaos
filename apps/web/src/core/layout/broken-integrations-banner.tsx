import { useBrokenSummary, useCurrentOrgSlug } from "@core/api";
import { useCurrentUser } from "@domain/auth";
import { Link } from "@tanstack/react-router";

/** Red banner shown when the current org has one or more broken MCP integrations.
 *  Owners + Admins only (the backend zeros the list for Builders). Click deep-links
 *  to the MCP Proxy settings page. */
export function BrokenIntegrationsBanner() {
  const { data: user } = useCurrentUser();
  const { data: summary } = useBrokenSummary();
  const slug = useCurrentOrgSlug();
  if (!user || !summary || !slug) return null;
  const currentMembership = user.memberships.find((m) => m.slug === slug);
  if (!currentMembership) return null;
  const orgEntry = summary.orgs.find((o) => o.org_id === currentMembership.org_id);
  if (!orgEntry || orgEntry.broken_integrations.length === 0) return null;
  const providers = orgEntry.broken_integrations.map((b) => b.provider).join(", ");
  return (
    <Link
      to="/orgs/$slug/settings/mcp-proxy"
      params={{ slug: currentMembership.slug }}
      className="block bg-red-100 border-b border-red-300 text-red-900 px-4 py-2 text-sm hover:bg-red-200"
      data-testid="broken-integrations-banner"
    >
      <span className="font-semibold">MCP integration disconnected:</span> {providers}. Reconnect in
      Org Settings → MCP Proxy.
    </Link>
  );
}
