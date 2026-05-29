# e2e — test conventions

> Conventions for writing and extending Playwright specs in `apps/e2e`.

## Module boundary rule

Specs share helpers only through `tests/_helpers.ts`. No cross-spec state or imports. Test helpers are never exported to other apps — all cross-app test machinery (isolation reset, seed) is driven through the backend's `/api/testing/*` HTTP surface, not through direct imports.
