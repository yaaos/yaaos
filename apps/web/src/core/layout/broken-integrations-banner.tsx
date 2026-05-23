import { useCurrentUser } from "@domain/auth";

/** Red banner shown when the current org has one or more broken MCP integrations.
 *  Owners + Admins only (the backend zeros the list for Builders). Click deep-links
 *  to the MCP Proxy settings page (renamed in M06 Phase 8 — was Integrations). */
export function BrokenIntegrationsBanner() {
  const { data } = useCurrentUser();
  if (!data) return null;
  const currentOrg = data.orgs.find((o) => o.slug === data.current_org_slug);
  if (!currentOrg || currentOrg.broken_integrations.length === 0) return null;
  const providers = currentOrg.broken_integrations.map((b) => b.provider).join(", ");
  return (
    <a
      href={`/orgs/${currentOrg.slug}/settings/mcp-proxy`}
      className="block bg-red-100 border-b border-red-300 text-red-900 px-4 py-2 text-sm hover:bg-red-200"
      data-testid="broken-integrations-banner"
    >
      <span className="font-semibold">MCP integration disconnected:</span> {providers}. Reconnect in
      Org Settings → MCP Proxy.
    </a>
  );
}
