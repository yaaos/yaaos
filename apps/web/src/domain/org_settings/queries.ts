import { apiFetch } from "@core/api";
import { useMutation, useQueryClient } from "@tanstack/react-query";

/** PATCH /api/orgs — currently surfaces `session_timeout_override`. */
export function useUpdateOrgSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: { session_timeout_override?: number | null }) =>
      apiFetch<{ slug: string; session_timeout_override: number | null }>("/api/orgs", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth", "me"] }),
  });
}
