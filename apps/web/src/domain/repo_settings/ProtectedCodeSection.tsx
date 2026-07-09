/**
 * Protected-code section — deny/allow mode (flipping it inverts what's
 * protected, so switching requires an `AlertDialog` confirm) + path sets
 * (gitignore-style globs + owners).
 */

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@shared/components/ui/alert-dialog";
import { Button } from "@shared/components/ui/button";
import { Label } from "@shared/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@shared/components/ui/radio-group";
import { Textarea } from "@shared/components/ui/textarea";
import { Plus, X } from "lucide-react";
import { useState } from "react";
import { UserMultiSelect } from "./UserMultiSelect";
import type { OrgMemberSummary } from "./queries";
import { type PathSetDraft, type RepoSettingsDraft, newPathSetDraft } from "./types";

export function ProtectedCodeSection({
  draft,
  setDraft,
  members,
}: {
  draft: RepoSettingsDraft;
  setDraft: (d: RepoSettingsDraft) => void;
  members: OrgMemberSummary[];
}) {
  const [pendingMode, setPendingMode] = useState<"allow" | "deny" | null>(null);

  function requestModeChange(next: string) {
    if (next === draft.protected_mode) return;
    setPendingMode(next as "allow" | "deny");
  }

  function updatePathSet(id: string, next: PathSetDraft) {
    setDraft({ ...draft, path_sets: draft.path_sets.map((p) => (p.id === id ? next : p)) });
  }

  function removePathSet(id: string) {
    setDraft({ ...draft, path_sets: draft.path_sets.filter((p) => p.id !== id) });
  }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold">Protected code</h3>

      <RadioGroup
        data-testid="repo-protected-mode"
        value={draft.protected_mode}
        onValueChange={requestModeChange}
      >
        <div className="flex items-center gap-2">
          <RadioGroupItem value="deny" id="protected-mode-deny" />
          <Label htmlFor="protected-mode-deny">Deny list — only matched paths are protected</Label>
        </div>
        <div className="flex items-center gap-2">
          <RadioGroupItem value="allow" id="protected-mode-allow" />
          <Label htmlFor="protected-mode-allow">
            Allow list — everything except matched paths is protected
          </Label>
        </div>
      </RadioGroup>

      <div className="flex flex-col gap-2">
        {draft.path_sets.map((pathSet) => (
          <div
            key={pathSet.id}
            data-testid={`repo-path-set-${pathSet.id}`}
            className="flex flex-col gap-2 rounded-md border border-border p-3"
          >
            <div className="flex items-start gap-2">
              <div className="flex flex-1 flex-col gap-1.5">
                <Label htmlFor={`path-set-globs-${pathSet.id}`}>Globs (one per line)</Label>
                <Textarea
                  id={`path-set-globs-${pathSet.id}`}
                  data-testid={`repo-path-set-globs-${pathSet.id}`}
                  value={pathSet.globsText}
                  onChange={(e) =>
                    updatePathSet(pathSet.id, { ...pathSet, globsText: e.target.value })
                  }
                  placeholder={"src/migrations/**\ninfra/**"}
                />
              </div>
              <Button
                variant="ghost"
                size="sm"
                aria-label="Remove path set"
                data-testid={`repo-path-set-remove-${pathSet.id}`}
                onClick={() => removePathSet(pathSet.id)}
              >
                <X className="w-4 h-4" aria-hidden />
              </Button>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`path-set-owners-${pathSet.id}`}>Owners</Label>
              <UserMultiSelect
                members={members}
                selected={pathSet.owner_user_ids}
                onChange={(ids) => updatePathSet(pathSet.id, { ...pathSet, owner_user_ids: ids })}
                placeholder="Choose owners…"
                data-testid={`repo-path-set-owners-${pathSet.id}`}
              />
            </div>
          </div>
        ))}
      </div>

      <Button
        variant="outline"
        size="sm"
        className="self-start"
        data-testid="repo-add-path-set"
        onClick={() => setDraft({ ...draft, path_sets: [...draft.path_sets, newPathSetDraft()] })}
      >
        <Plus className="w-4 h-4" aria-hidden />
        Add path set
      </Button>

      <AlertDialog
        open={pendingMode != null}
        onOpenChange={(open) => !open && setPendingMode(null)}
      >
        <AlertDialogContent data-testid="repo-protected-mode-confirm">
          <AlertDialogHeader>
            <AlertDialogTitle>Switch protected-code mode?</AlertDialogTitle>
            <AlertDialogDescription>This inverts what&apos;s protected.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setPendingMode(null)}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              data-testid="repo-protected-mode-confirm-switch"
              onClick={() => {
                if (pendingMode) setDraft({ ...draft, protected_mode: pendingMode });
                setPendingMode(null);
              }}
            >
              Switch
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
