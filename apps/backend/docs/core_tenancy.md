# core/tenancy

> Owns the `orgs` and `memberships` tables — the IAM access graph that answers "who is in which org at what role."

## Scope

- **Owns:** `orgs` + `memberships` tables. Every row in every other table is org-scoped via `org_id`.
- **Does NOT own:** invitations, SSO config detail, coding agents, VCS state. Those remain in [`domain/orgs`](domain_orgs.md).
- **Boundary:** receives `user_id` (bare UUID) + org slug; emits value objects (`OrgRef`, `AuthOrg`, `MembershipView`). Never returns Row types.

## Why / invariants

- **Split rationale.** `domain/orgs` conflated the membership/role graph (authz data the core layer needs) with org feature aggregates (invitation flow, SSO config, VCS binding). Splitting moves the access graph into core so `core/sessions` can resolve authz without importing a domain module.
- **`core/tenancy` must NOT import `core/identity`.** Membership stores `user_id` as a bare UUID/FK. The provisioning direction is `identity → tenancy` (login creates a membership); the reverse would forge a new intra-core cycle. `core/auth` is the only dependency below `core/tenancy` in the intra-core layer order: `core/auth < core/tenancy < core/identity < core/sessions`.
- **SSO authz columns denormalized on `orgs`.** `sso_enabled` and `sso_exempt_owner_user_id` are copied from `sso_configs` onto `orgs` for fast middleware access. `domain/orgs/sso.upsert_config` calls `set_sso_authz_for_org` to keep them in sync.
- **VOs only in public API.** All primitives return Pydantic value objects; `OrgRow`/`MembershipRow` never appear in `__all__` or cross a module boundary.

## Gotchas

- Do not import `MembershipRow` or `OrgRow` from `core.tenancy.models` outside this module. Use the service primitives instead — they return VOs.
- `resolve_auth_org` returns `None` for both "org not found" and "user not a member" — callers should not distinguish.
- `sso_enabled` / `sso_exempt_owner_user_id` on `OrgRow` lag one transaction behind `sso_configs` until `set_sso_authz_for_org` is called; always update both in the same transaction.

## Vocabulary

- **OrgRef** — caller-agnostic org identity: `org_id`, `slug`, `name`.
- **OrgFullView** — extended org projection including all feature columns (`session_timeout_override`, `workspace_provider`, `registered_iam_arn`, `aws_region`, `vcs_plugin_id`, `vcs_settings`). Returned by `get_org_full`, `get_org_full_by_slug`, `update_org_fields`, etc. Used by any module that needs to read org feature columns without importing `OrgRow` — including `domain/orgs`, `domain/intake`, and `plugins/github`.
- **OrgMembershipInfo** — per-org membership projection: `user_id`, `org_id`, `role`, `handle`. Returned by `get_membership_info` and `list_memberships_for_org`.
- **AuthOrg** — per-caller authz projection: `org_id`, `slug`, `role`, `sso_enabled`, `sso_exempt_owner_user_id`, `session_timeout_override`. Consumed by `core/sessions.require()` for the full auth gate in one lookup.
- **MembershipView** — user's membership list item: `org_id`, `slug`, `org_name`, `role`, `handle`.

## Entry points

- `apps/backend/app/core/tenancy/__init__.py` — public interface.
- `apps/backend/app/core/tenancy/service.py` — all primitives.
- `apps/backend/app/core/tenancy/models.py` — `OrgRow`, `MembershipRow`.
- `apps/backend/app/core/tenancy/repository.py` — raw ORM access (intra-module only).
- `apps/backend/app/core/tenancy/test/test_tenancy_service.py` — service tests.

Test teardown that hard-deletes an org row uses `delete_org` from `app.testing.seed` — there is no production org-deletion flow.
