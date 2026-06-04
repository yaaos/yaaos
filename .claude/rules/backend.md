---
paths:
  - "apps/backend/**"
---

# Backend conventions

Authoritative for any change under `apps/backend/`. Follow them — they own the decisions that look arbitrary in the code. Mechanical rules (imports, layering, `__all__`, table access) are enforced by `tach` / `bin/sync_modules` / `bin/check_table_access` / `semgrep` at CI; the docs below carry the judgment conventions those checkers can't.

@../../apps/backend/docs/architecture.md
@../../apps/backend/docs/patterns.md
