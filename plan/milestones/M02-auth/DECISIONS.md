# M02 — decisions made during autonomous run

> Append-only log of decisions made when the spec was ambiguous and certainty was below 3 of 5. Per [START_HERE.md § Decision protocol](START_HERE.md#decision-protocol).

## Format

Each entry:

```
### <Phase N> — <one-line decision summary>

- **Certainty**: <1 or 2>/5
- **Decision**: <what was chosen>
- **Alternatives considered**: <brief>
- **Why this one**: <one line>
- **Reversal cost**: <low/medium/high — how painful to undo later>
```

Keep entries terse. The user reads this at the end of the run; volume = friction.

## Entries

<!-- Append below. Do not edit prior entries. -->

### Phase 1 — M02 migration named `010_create_all_m02` (not `002_…`)

- **Certainty**: 2/5
- **Decision**: Registered the M02 create-all migration as `010_create_all_m02` in `core/database/service.py`. The spec said `002_create_all_m02`, but `002_github_settings_slug` (and 003–009) already exist from M01 maintenance migrations, so `002` would collide and break ordering.
- **Alternatives considered**: rename existing `002_…` (would invalidate every applied schema_migrations row); name it `m02_create_all` without a number (breaks the existing numeric ordering convention).
- **Why this one**: keeps strict monotonic version ordering with zero impact on already-applied DBs.
- **Reversal cost**: low — version string is only used as a registry key.

### Phase 1 — `audit_entries` gains `actor_user_id` + `actor_workspace_id` columns

- **Certainty**: 2/5
- **Decision**: The M02 migration adds two nullable UUID columns to `audit_entries` so the additive `user` / `workspace` `ActorKind` values round-trip through the audit row (existing `actor_login` / `actor_agent_id` can't carry them). `sso` actor kind uses only `actor_login` (the IdP-asserted email) since no domain id exists.
- **Alternatives considered**: pack the ids into the `payload` JSONB (cheap but loses queryability by who-did-what); add a single polymorphic `actor_subject_id` column (loses the type tagging without an extra discriminator).
- **Why this one**: keeps the columnar shape that existing per-entity audit helpers already use; nullable adds are additive and idempotent under `ADD COLUMN IF NOT EXISTS`.
- **Reversal cost**: low — additive nullable columns can be dropped without breaking reads.
