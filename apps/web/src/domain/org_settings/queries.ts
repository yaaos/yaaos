import { apiFetch } from "@core/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export interface OrgSettings {
  slug: string;
  session_timeout_override: number | null;
  registered_iam_arn: string | null;
  aws_region: string | null;
}

export function useOrgSettings() {
  return useQuery<OrgSettings>({
    queryKey: ["org-settings"],
    queryFn: () => apiFetch<OrgSettings>("/api/orgs"),
  });
}

/** PATCH /api/orgs — session timeout + workspace config. */
export function useUpdateOrgSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (
      body: Partial<{
        session_timeout_override: number | null;
        registered_iam_arn: string | null;
        aws_region: string | null;
      }>,
    ) =>
      apiFetch<OrgSettings>("/api/orgs", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["auth", "me"] });
      qc.invalidateQueries({ queryKey: ["org-settings"] });
    },
  });
}
