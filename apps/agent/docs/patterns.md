# WorkspaceAgent — coding conventions

> Conventions for writing and extending Go packages inside `apps/agent`.

## Module boundary rule

Test helpers must not cross package boundaries. A helper used only by a package's own tests stays private to that package. Cross-package test setup is not used here — the agent has no shared test-helper surface.
