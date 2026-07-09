/**
 * Protected-code section — deny/allow mode (flipping it inverts what's
 * protected, so switching requires an `AlertDialog` confirm) + path sets
 * (name + gitignore-style globs + owners).
 *
 * Path-set rows are compact: name label + Edit + Delete. Edit (and Add)
 * opens a right-anchored Sheet. Delete confirms via an AlertDialog using the
 * locked destructive-confirm copy pattern.
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
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@shared/components/ui/radio-group";
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@shared/components/ui/sheet";
import { Textarea } from "@shared/components/ui/textarea";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
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
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingSet, setEditingSet] = useState<PathSetDraft | null>(null);
  const [pendingDelete, setPendingDelete] = useState<PathSetDraft | null>(null);

  function requestModeChange(next: string) {
    if (next === draft.protected_mode) return;
    setPendingMode(next as "allow" | "deny");
  }

  function openAdd() {
    setEditingSet(newPathSetDraft());
    setEditorOpen(true);
  }

  function openEdit(pathSet: PathSetDraft) {
    setEditingSet({ ...pathSet });
    setEditorOpen(true);
  }

  function handleEditorSave(saved: PathSetDraft) {
    const exists = draft.path_sets.some((p) => p.id === saved.id);
    if (exists) {
      setDraft({
        ...draft,
        path_sets: draft.path_sets.map((p) => (p.id === saved.id ? saved : p)),
      });
    } else {
      setDraft({ ...draft, path_sets: [...draft.path_sets, saved] });
    }
    setEditorOpen(false);
  }

  function handleDelete(id: string) {
    setDraft({ ...draft, path_sets: draft.path_sets.filter((p) => p.id !== id) });
    setPendingDelete(null);
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

      <div className="flex flex-col gap-1">
        {draft.path_sets.map((pathSet) => (
          <div
            key={pathSet.id}
            data-testid={`repo-path-set-row-${pathSet.id}`}
            className="flex items-center gap-2 rounded-md border border-border px-3 py-2"
          >
            <span className="flex-1 truncate text-sm">
              {pathSet.name || <span className="text-muted-foreground italic">Unnamed</span>}
            </span>
            <Button
              variant="ghost"
              size="sm"
              aria-label={`Edit path set ${pathSet.name}`}
              data-testid={`repo-path-set-edit-${pathSet.id}`}
              onClick={() => openEdit(pathSet)}
            >
              <Pencil className="w-4 h-4" aria-hidden />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              aria-label={`Delete path set ${pathSet.name}`}
              data-testid={`repo-path-set-delete-${pathSet.id}`}
              onClick={() => setPendingDelete(pathSet)}
            >
              <Trash2 className="w-4 h-4" aria-hidden />
            </Button>
          </div>
        ))}
      </div>

      <Button
        variant="outline"
        size="sm"
        className="self-start"
        data-testid="repo-add-path-set"
        onClick={openAdd}
      >
        <Plus className="w-4 h-4" aria-hidden />
        Add path set
      </Button>

      {/* Mode-flip confirm */}
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

      {/* Delete confirm */}
      <AlertDialog
        open={pendingDelete != null}
        onOpenChange={(open) => !open && setPendingDelete(null)}
      >
        <AlertDialogContent data-testid="repo-path-set-delete-confirm">
          <AlertDialogHeader>
            <AlertDialogTitle>
              Delete &ldquo;{pendingDelete?.name || "Unnamed"}&rdquo;?
            </AlertDialogTitle>
            <AlertDialogDescription>This can&apos;t be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setPendingDelete(null)}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              data-testid="repo-path-set-delete-confirm-action"
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => pendingDelete && handleDelete(pendingDelete.id)}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Path set Sheet editor */}
      {editingSet && (
        <PathSetEditorSheet
          open={editorOpen}
          onOpenChange={setEditorOpen}
          pathSet={editingSet}
          members={members}
          onSave={handleEditorSave}
        />
      )}
    </div>
  );
}

function PathSetEditorSheet({
  open,
  onOpenChange,
  pathSet,
  members,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pathSet: PathSetDraft;
  members: OrgMemberSummary[];
  onSave: (saved: PathSetDraft) => void;
}) {
  const [draft, setDraft] = useState<PathSetDraft>(pathSet);

  // Re-seed whenever the sheet opens for a (possibly different) path set.
  useEffect(() => {
    if (open) setDraft(pathSet);
  }, [open, pathSet]);

  const canSave = draft.name.trim().length > 0;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        data-testid="repo-path-set-editor"
        className="sm:max-w-lg overflow-y-auto"
      >
        <SheetHeader>
          <SheetTitle>Path set</SheetTitle>
        </SheetHeader>
        <div className="flex flex-col gap-4 py-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="path-set-name">Name</Label>
            <Input
              id="path-set-name"
              data-testid="repo-path-set-name"
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              placeholder="e.g. Infra paths"
              maxLength={100}
            />
            {draft.name.trim().length === 0 && (
              <p className="text-xs text-muted-foreground">Name is required.</p>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="path-set-globs">Globs (one per line)</Label>
            <Textarea
              id="path-set-globs"
              data-testid={`repo-path-set-globs-${draft.id}`}
              value={draft.globsText}
              onChange={(e) => setDraft({ ...draft, globsText: e.target.value })}
              placeholder={"src/migrations/**\ninfra/**"}
              rows={6}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="path-set-owners">Owners</Label>
            <UserMultiSelect
              members={members}
              selected={draft.owner_user_ids}
              onChange={(ids) => setDraft({ ...draft, owner_user_ids: ids })}
              placeholder="Choose owners…"
              data-testid={`repo-path-set-owners-${draft.id}`}
            />
          </div>
        </div>
        <SheetFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            data-testid="repo-path-set-editor-save"
            disabled={!canSave}
            onClick={() => {
              onSave(draft);
            }}
          >
            Save
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
