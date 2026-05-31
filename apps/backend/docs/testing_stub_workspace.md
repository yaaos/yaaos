# testing/stub_workspace

> Wrapper provider that fakes any `WorkspaceProvider` so tests run offline without `git clone`.

## Purpose

When `YAAOS_WORKSPACE_STUB` is set, `app/web.py` calls `wrap_all_registered_workspace_providers()` after `bootstrap()`, replacing every registry entry with a `StubWorkspaceProvider`. `provision` creates an empty tempdir with a marker; no git clone, no VCS plugin lookup. Excluded from production wheel builds.

## Public interface

- `StubWorkspaceProvider`
- `wrap_all_registered_workspace_providers`

No HTTP routes. No `bootstrap()` — wired from `app/web.py`.

## Module architecture

### `StubWorkspaceProvider(wrapped)`

Mirrors `meta` from the real provider. Wrapped instance is held but never delegated to.

- **`provision`** — `tempfile.mkdtemp(prefix="yaaos-ws-stub-")`, writes `.yaaos-workspace` marker with `stub=true`. Returns `{"working_dir": working_dir}`. **No `git clone`, no token.**
- **`run_coding_agent_cli`** — no-op; returns `CodingAgentCliResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=0)`. Tests using coding-agent flows should go through `stub_coding_agent`, which short-circuits before any workspace call.
- **`destroy`** — `shutil.rmtree(working_dir, ignore_errors=True)`. Idempotent.
- **`health_check`** — `healthy=True, message="stub mode"`.

### `wrap_all_registered_workspace_providers()`

Reads the current `WorkspaceRegistry` via `current_workspace_registry()`, builds a fresh `WorkspaceRegistry` with each entry wrapped, and binds it via `bind_workspace_registry()`. Idempotent — already-wrapped entries are kept as-is. Future workspace providers (Docker, K8s) require zero changes here.

### Why a wrapper, not a free-standing fake

Mirroring `meta.id` routes the stub under the real provider's key — no test needs to know which provider is active. Holding the wrapped reference means Protocol surface changes force a corresponding update here; the type checker catches drift.

### Companion: stub_coding_agent

Both stubs activate together (`YAAOS_CODING_AGENT_STUB` + `YAAOS_WORKSPACE_STUB`). `app/web.py` order: real plugins bootstrap → testing wrappers replace registry entries. See [testing_stub_coding_agent.md](testing_stub_coding_agent.md).

## Data owned

None. Empty tempdir per workspace, cleaned via `destroy`. The `workspaces` row is owned by `core/workspace`.

## How it's tested

`app/testing/stub_workspace/test/test_stub.py` — `provision` returns real tempdir with marker but no git activity; `run_coding_agent_cli` returns canned result; `destroy` cleans up; `health_check` reports stub mode; `wrap_all_registered_workspace_providers` is idempotent.

Exercised by every Playwright spec (`YAAOS_WORKSPACE_STUB=1`).
