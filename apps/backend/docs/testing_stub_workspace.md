# testing/stub_workspace

> Wrapper provider that fakes any `WorkspaceProvider` so tests run offline without `git clone`.

## Purpose

Test-only `WorkspaceProvider`. When `YAAOS_WORKSPACE_STUB` is set, `app/web.py` calls `wrap_all_registered_workspace_providers()`, which walks `core/workspace`'s registry and replaces every entry with a `StubWorkspaceProvider` wrapping the real one. `provision` creates an empty tempdir + marker; no git clone, no VCS plugin lookup. `run_coding_agent_cli` returns a canned empty result; `destroy` rmtrees. Mirrors `testing/stub_coding_agent`; both activate together in e2e. Excluded from production wheel builds.

## Public interface

- `StubWorkspaceProvider`
- `wrap_all_registered_workspace_providers`

No HTTP routes. No `bootstrap()` — wired from `app/web.py`.

## Module architecture

### `StubWorkspaceProvider(wrapped)`

Mirrors `meta` from the real provider. Wrapped provider is held but never delegated to — every method either no-ops or returns canned values.

- **`provision`** — `tempfile.mkdtemp(prefix="yaaos-ws-stub-")`, writes a `.yaaos-workspace` marker with `stub=true`, `plugin_id`, `repo`, `sha`. Best-effort marker. Returns `{"working_dir": working_dir}` — same shape `core/workspace`'s persistence expects. **No `git clone`, no `vcs.get_installation_token`.** Tests don't need either; the stub coding agent never reads the working dir.
- **`run_coding_agent_cli`** — no-op; canned `CodingAgentCliResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=0)`. Tests reaching this layer should exercise coding-agent flows through `stub_coding_agent`, which short-circuits before any workspace call. The no-op preserves Protocol completeness for tests that drive the workspace plugin directly without a coding agent.
- **`destroy`** — `shutil.rmtree(working_dir, ignore_errors=True)` if present. Idempotent.
- **`health_check`** — `healthy=True, message="stub mode"`.

### `wrap_all_registered_workspace_providers()`

Calls `list_workspace_providers()` + `clear_workspace_providers()` + `register_workspace_provider()` to swap every entry for a stub wrapping it. Idempotent. Logs `stub_workspace.wrapped_all` with the count. Future workspace providers (Docker, K8s) require zero changes here.

### Why a wrapper, not a free-standing fake

Mirroring `meta.id` means consumers route to the stub under the real provider's key — no test has to know which provider is active. Holding the wrapped reference also means any change to the real Protocol surface forces a corresponding change here; the type checker catches drift.

### Companion: stub_coding_agent

`testing_stub_coding_agent.md` covers the matching coding-agent stub. Both activate together (`YAAOS_CODING_AGENT_STUB` + `YAAOS_WORKSPACE_STUB`). `app/web.py` order: real plugins bootstrap → testing wrappers replace registry entries.

## Data owned

None. Empty tempdir per workspace, cleaned via `destroy`. The `workspaces` row is owned by `core/workspace`.

## How it's tested

Unit tests in `app/testing/stub_workspace/test/`:

- `test_stub.py` — `provision` returns a real tempdir with marker but no git activity; `run_coding_agent_cli` returns the canned empty result; `destroy` removes the tempdir; `health_check` reports stub mode; `wrap_all_registered_workspace_providers` is idempotent and replaces the in-process provider under its existing `plugin_id`.

Exercised end-to-end by every Playwright spec in `apps/e2e/`, running with `YAAOS_WORKSPACE_STUB=1` alongside the coding-agent stub.
