/**
 * Org-membership picklist for the Repos page's notify/owner multi-selects.
 * `/api/memberships` is owned by `domain/orgs` (see `domain/org_settings`'s
 * Members page) but this module hits it directly — same pattern as
 * `domain/pipeline_settings/queries.ts`'s coding-agent picklists: a second,
 * independent consumer of the same REST surface, no cross-domain import.
 */

import { apiFetch } from "@core/api/public/client";
import { useSuspenseQuery } from "@tanstack/react-query";

export interface OrgMemberSummary {
  user_id: string;
  handle: string;
  display_name: string;
}

/** Active org members — the notify (`Schedule.notify_user_ids`) and owner
 *  (`ProtectedPathSet.owner_user_ids`) multi-selects. */
export function useOrgMembers() {
  return useSuspenseQuery<OrgMemberSummary[]>({
    queryKey: ["org-members"],
    queryFn: () => apiFetch<OrgMemberSummary[]>("/api/memberships"),
    staleTime: 10_000,
  });
}
