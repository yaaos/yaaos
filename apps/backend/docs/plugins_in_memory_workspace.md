# plugins/in_memory_workspace

> Tempdir-backed `WorkspaceProvider`. Clones repos onto the host filesystem and runs coding-agent CLIs in-process. No isolation — for single-tenant deployments where the host trusts the coding-agent CLI.

## Scope

The only concrete `core/workspace.WorkspaceProvider` in . Implements `provision`, `run_coding_agent_cli`, `destroy`, `health_check`. Provider singleton holds no state — per-workspace state lives in the `plugin_state` dict returned from `provision`.

## Module architecture

### `provision(spec)`

1. `tempfile.mkdtemp(prefix="yaaos-ws-")`.
2. Write chmod-0700 askpass script in a sibling tempfile.
3. `vcs.get_installation_token(spec.repo.plugin_id, spec.org_id)` — token lives only in process memory and briefly in subprocess env (via `GIT_ASKPASS`/`YAAOS_GIT_TOKEN`; never on argv).
4. `git clone --depth=1 --branch <branch>` from the VCS plugin's `clone_url`.
5. If `spec.sha` is set and not `"HEAD"`: fetch + checkout the exact PR head sha.
6. If `spec.base_sha` is set: `git fetch --depth=1 origin <base_sha>` (best-effort). Brings the base commit into the shallow store so agents can `git diff <base_sha>..HEAD` without the intermediate chain.
7. Write `.yaaos-workspace` marker (best-effort).

Returns `{"working_dir": working_dir}`. Failure rmtrees before re-raising.

### `run_coding_agent_cli`

`asyncio.create_subprocess_exec` with `cwd=working_dir`, `start_new_session=True` (enables process-group kill). Two modes:
- No `on_stream_line` → buffered via `communicate(input=stdin, timeout=...)`.
- With `on_stream_line` → stream stdout line-by-line; stderr buffered.

Two kill paths share `_kill_process_group` (SIGTERM → 2s grace → SIGKILL):
- **Timeout** — returns `CodingAgentCliResult(timed_out=True)`.
- **Caller cancel** — kills, drains (5s cap), re-raises `CancelledError`. Without this, the CLI would keep running until its own timeout after the workflow transitions to `cancelled`.

Provider does not interpret `argv` or `stdout`; schema-aware logic lives in the coding-agent plugin.

### `destroy`

`shutil.rmtree(working_dir, ignore_errors=True)`. Idempotent.

### Test-mode wrapping

Never branches on env vars. When `YAAOS_WORKSPACE_STUB` is set, `app/web.py` calls `testing.stub_workspace.wrap_all_registered_workspace_providers()` after `bootstrap()`. See [testing_stub_workspace.md](testing_stub_workspace.md).

## Data owned

None. Per-workspace state is the tempdir + `{"working_dir": ...}` dict persisted by `core/workspace` in the `workspaces` table.

## How it's tested

Unit tests in `app/plugins/in_memory_workspace/test/test_provider.py` — fake `VCSPlugin` + local bare git repo as clone source; covers full `git clone` → `fetch` → `checkout` path, `run_coding_agent_cli` (exit codes, stdin, timeout/SIGKILL), and `destroy` idempotency.

Exercised indirectly by every backend integration test running a reviewer flow through the real plugin stack.
