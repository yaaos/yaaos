/**
 * Editable draft shapes for the Repos settings page's per-repo config form
 * (protected code + PR auto-approval — the two sections that share one
 * `PUT /api/repos/settings` whole-section-replace call).
 *
 * `RepoConfigView` (see `core/api/public/queries`) is what the backend
 * returns; the form works over a richer local "draft" shape — path-set
 * globs flattened to one-per-line textarea text, and
 * `auto_approve_conditions` (a loosely-typed dict on the wire) narrowed to
 * the four known fields. `configToDraft` / `draftToSpec` convert at the
 * form's boundary.
 *
 * Trigger bindings are NOT part of this draft — `add_binding`/
 * `remove_binding` are separate, immediately-committed calls (see
 * `TriggersSection.tsx`), not part of the settings whole-section replace.
 */

import type {
  ProtectedPathSetView,
  RepoConfigView,
  RepoSettingsSpecBody,
} from "@core/api/public/queries";

export interface PathSetDraft {
  /** Server id when loaded from an existing config; client-minted
   *  (`crypto.randomUUID()`) for a not-yet-saved row — `ProtectedPathSet.id`
   *  is required on the wire, so a new row must mint one before Save. */
  id: string;
  globsText: string;
  owner_user_ids: string[];
}

export interface AutoApproveConditionsDraft {
  no_blocker: boolean;
  no_should_fix: boolean;
  no_nit: boolean;
  all_confirmed_fixed: boolean;
}

export interface RepoSettingsDraft {
  protected_mode: "allow" | "deny";
  path_sets: PathSetDraft[];
  auto_approve_enabled: boolean;
  auto_approve_conditions: AutoApproveConditionsDraft;
}

function pathSetToDraft(pathSet: ProtectedPathSetView): PathSetDraft {
  return {
    id: pathSet.id,
    globsText: pathSet.globs.join("\n"),
    owner_user_ids: pathSet.owner_user_ids,
  };
}

export function configToDraft(config: RepoConfigView): RepoSettingsDraft {
  const conditions = config.auto_approve_conditions as Partial<AutoApproveConditionsDraft>;
  return {
    protected_mode: config.protected_mode,
    path_sets: config.protected_path_sets.map(pathSetToDraft),
    auto_approve_enabled: config.auto_approve_enabled,
    auto_approve_conditions: {
      no_blocker: conditions.no_blocker ?? false,
      no_should_fix: conditions.no_should_fix ?? false,
      no_nit: conditions.no_nit ?? false,
      all_confirmed_fixed: conditions.all_confirmed_fixed ?? false,
    },
  };
}

export function draftToSpec(draft: RepoSettingsDraft): RepoSettingsSpecBody {
  return {
    protected_mode: draft.protected_mode,
    protected_path_sets: draft.path_sets.map((p) => ({
      id: p.id,
      globs: p.globsText
        .split("\n")
        .map((g) => g.trim())
        .filter(Boolean),
      owner_user_ids: p.owner_user_ids,
    })),
    auto_approve_enabled: draft.auto_approve_enabled,
    // The wire field is a loosely-typed dict (`domain/findings.AutoApproveConditions`
    // owns the real shape server-side) — spread into a plain record to satisfy it.
    auto_approve_conditions: { ...draft.auto_approve_conditions } as Record<string, unknown>,
  };
}

export function newPathSetDraft(): PathSetDraft {
  return { id: crypto.randomUUID(), globsText: "", owner_user_ids: [] };
}
