/**
 * PR auto-approval section — a `Switch` gate plus four plain-English
 * conditions (`domain/findings.AutoApproveConditions`). Auto-approve skips
 * yaaos-authored PRs server-side (GitHub forbids self-approval) — no client
 * hint needed, the condition set is the same regardless.
 */

import { Checkbox } from "@shared/components/ui/checkbox";
import { Label } from "@shared/components/ui/label";
import { Switch } from "@shared/components/ui/switch";
import type { RepoSettingsDraft } from "./types";

const CONDITIONS: Array<{
  key: keyof RepoSettingsDraft["auto_approve_conditions"];
  label: string;
}> = [
  { key: "no_blocker", label: "No open blocker findings" },
  { key: "no_should_fix", label: "No open should-fix findings" },
  { key: "no_nit", label: "No open nit findings" },
  { key: "all_confirmed_fixed", label: "Every posted finding confirmed fixed" },
];

export function AutoApprovalSection({
  draft,
  setDraft,
}: {
  draft: RepoSettingsDraft;
  setDraft: (d: RepoSettingsDraft) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold">PR auto-approval</h3>
      <div className="flex items-center gap-2">
        <Switch
          id="repo-auto-approve-enabled"
          data-testid="repo-auto-approve-enabled"
          checked={draft.auto_approve_enabled}
          onCheckedChange={(checked) => setDraft({ ...draft, auto_approve_enabled: checked })}
        />
        <Label htmlFor="repo-auto-approve-enabled">
          Auto-approve when every condition below is met
        </Label>
      </div>
      {draft.auto_approve_enabled && (
        <div className="flex flex-col gap-2 pl-6">
          {CONDITIONS.map(({ key, label }) => (
            <div key={key} className="flex items-center gap-2">
              <Checkbox
                id={`repo-auto-approve-${key}`}
                data-testid={`repo-auto-approve-${key}`}
                checked={draft.auto_approve_conditions[key]}
                onCheckedChange={(checked) =>
                  setDraft({
                    ...draft,
                    auto_approve_conditions: {
                      ...draft.auto_approve_conditions,
                      [key]: checked === true,
                    },
                  })
                }
              />
              <Label htmlFor={`repo-auto-approve-${key}`}>{label}</Label>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
