# plugins/in_process_workspace

> Tempdir-backed `WorkspaceProvider`. Clones repos onto the host filesystem and runs coding-agent CLIs in-process. POC only — no isolation.

## Purpose

The only concrete `core/workspace.WorkspaceProvider` in M01. Implements `provision`, `run_coding_agent_cli`, `destroy`, `health_check`. Provisions by `mkdtemp` + `git clone --depth=1`, runs coding-agent CLIs in that tempdir, rmtrees on destroy. Provider singleton holds no state — per-workspace state lives in the `plugin_state` dict returned from `provision` and passed back into every call.

## Public interface

- Singleton `InProcessWorkspaceProvider` registered into `core/workspace` at `bootstrap()`.
- Side-effect import of `web.py` mounts routes (prefix `/api/in_process`):
  - `GET /health` — `{healthy, message, checked_at}`. Tempdir is always available; returns healthy unconditionally.
- Domain code goes through `core/workspace`'s registry and the abstract `Workspace` handle.

## Module architecture

### `provision(spec)` — git clone with short-lived auth

`spec: WorkspaceSpec` carries `repo` (`plugin_id` + `external_id`), `sha`, `branch_name`, `base_sha`, `base_branch`, `org_id`. `org_id` required — can't mint a clone token without it.

1. `tempfile.mkdtemp(prefix="yaaos-ws-")`.
2. `_write_askpass()` — chmod 0700 askpass script in a sibling tempfile (outside `working_dir` because git clone requires an empty target).
3. `vcs.get_installation_token(spec.repo.plugin_id, spec.org_id)` — fresh token via the VCS plugin registry. Lives only in the Python process and briefly in the subprocess env.
4. Build clone URL from `plugin_id` + `external_id`. GitHub: `https://github.com/{external_id}.git`. Unknown plugin id raises `WorkspaceProvisionError`.
5. Subprocess env: copy of `os.environ` plus `GIT_ASKPASS`, `GIT_TERMINAL_PROMPT=0`, `YAAOS_GIT_TOKEN`. Token never on argv — git asks via the askpass script.
6. `git clone --depth=1 --branch <branch_name|HEAD>` — shallow clone of head branch tip.
7. If `spec.sha` set and not `"HEAD"`: `git fetch --depth=1 origin <sha>` then `git checkout <sha>`. Branch may have advanced; agents must see the PR's head sha.
8. If `spec.base_sha` set: `git fetch --depth=1 origin <base_sha>` (best-effort, logged-and-continued on failure). Brings the base commit as an orphan in the shallow store so subagents can run `git diff <base_sha>..HEAD` — diff works on tree endpoints without needing the intermediate chain. Whatever branch the PR targets (not necessarily `main`).
9. Write a `.yaaos-workspace` marker file (best-effort).
10. `finally` unlinks the askpass. Provision failures rmtree the working_dir before re-raising.

Returns `{"working_dir": working_dir}` — becomes `Workspace.plugin_state`. Consumers never see the path; they go through the `Workspace` handle.

### `run_coding_agent_cli`

Lets a coding-agent plugin run a CLI inside the workspace. Provider owns subprocess lifecycle so the coding-agent plugin stays vendor-only (see `plugins_claude_code.md` and `core/workspace` docs).

1. Read `working_dir` from `plugin_state`. Missing/vanished → `WorkspaceExecError`.
2. `asyncio.create_subprocess_exec` with `cwd=working_dir`, `start_new_session=True` (so SIGKILL can target the process group if the agent spawns children).
3. `asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout_seconds)`. On `TimeoutError`: `os.killpg(SIGTERM)` → 2s grace → `os.killpg(SIGKILL)`. Then drain.
4. Return `CodingAgentCliResult`. Bytes decoded `errors="replace"` so partial UTF-8 never crashes the caller.

Provider does not interpret `argv` or `stdout`; schema-aware logic lives in the coding-agent plugin.

### `destroy`

`shutil.rmtree(working_dir, ignore_errors=True)`. Idempotent — missing key, missing directory, or partial state all no-op. Logs `workspace.in_process.destroyed` on success.

### `health_check`

Always `healthy=True, message="ok"` in M01. Tempdir is part of the host filesystem; nothing to probe.

### Internal helpers

- `_clone_url_for(plugin_id, external_id)` — builds HTTPS URL. GitHub only; raises for unknown.
- `_write_askpass()` — chmod 0700 askpass in a sibling tempfile.
- `_git_env_with_token(askpass_path, token)` — env dict.
- `_run_subprocess(argv, env, timeout_seconds)` — setup-time git invocations. Same SIGTERM/SIGKILL dance.

### Test-mode wrapping

This file never branches on test env vars. When `YAAOS_WORKSPACE_STUB` is set, `app/main.py` calls `testing.stub_workspace.wrap_all_registered_workspace_providers()` after `bootstrap()`. See `testing_stub_workspace.md`.

## Data owned

None. Per-workspace state is the tempdir plus the `{"working_dir": ...}` dict that `core/workspace` persists in the `workspaces` table.

## How it's tested

Unit tests in `app/plugins/in_process_workspace/test/`:

- `test_provider.py` — fake `VCSPlugin` + a **local bare git repo** as the clone source so the full `git clone` → `fetch` → `checkout` path runs without network. Also covers `run_coding_agent_cli` against trivial `/bin/sh -c` subprocesses (exit codes, stdin piping, timeout-triggered SIGKILL) and `destroy` idempotency.

Exercised indirectly by every backend integration test running a reviewer review through the real plugin stack against fake-github.
