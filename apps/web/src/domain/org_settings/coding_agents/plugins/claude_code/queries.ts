import { apiFetch } from "@core/api/public/client";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

// ── Per-repo skill name ───────────────────────────────────────────────────────

export interface RepoSkillRow {
  repo_external_id: string;
  skill_name: string | null;
}

/** GET /api/claude_code/repos — live GitHub repos joined with stored skill names. */
export function useClaudeCodeRepos() {
  return useSuspenseQuery<RepoSkillRow[]>({
    queryKey: ["claude_code", "repos"],
    queryFn: async () => {
      const body = await apiFetch<{ repos: RepoSkillRow[] }>("/api/claude_code/repos");
      return body.repos;
    },
    staleTime: 30_000,
  });
}

/** PUT /api/claude_code/repos/{repo_external_id} — write one repo's skill name. */
export function useSetRepoSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      repoExternalId,
      skillName,
    }: { repoExternalId: string; skillName: string | null }) =>
      apiFetch<RepoSkillRow>(`/api/claude_code/repos/${encodeURIComponent(repoExternalId)}`, {
        method: "PUT",
        body: JSON.stringify({ skill_name: skillName }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["claude_code", "repos"] }),
  });
}

export interface ByokProviderStatus {
  provider: string;
  status: "configured" | "not_set";
  last_validated_at: string | null;
  last_used_at: string | null;
  updated_at: string | null;
}

export function useByokAnthropicStatus() {
  return useSuspenseQuery<ByokProviderStatus | null>({
    queryKey: ["byok", "anthropic"],
    queryFn: async () => {
      const rows = await apiFetch<ByokProviderStatus[]>("/api/api-keys");
      return rows.find((r) => r.provider === "anthropic") ?? null;
    },
    staleTime: 10_000,
  });
}

export function useSetByokAnthropic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (value: string) =>
      apiFetch<{ status: string }>("/api/api-keys/anthropic", {
        method: "POST",
        body: JSON.stringify({ value }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["byok"] }),
  });
}

export function useValidateByokAnthropic() {
  return useMutation({
    mutationFn: () =>
      apiFetch<{ valid: boolean }>("/api/api-keys/anthropic/validate", { method: "POST" }),
  });
}

export function useClearByokAnthropic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<{ removed: boolean }>("/api/api-keys/anthropic", { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["byok"] }),
  });
}
