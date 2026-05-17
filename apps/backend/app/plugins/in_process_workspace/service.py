"""In-process workspace provider — tempdir-backed, no real isolation. POC only.

Provisioning does a `git clone --depth=1` of the repo at the requested sha,
using a freshly-issued installation token from the registered VCS plugin and
`GIT_ASKPASS` so the token never appears in argv or on disk. The token lives
only in the Python process and briefly in the subprocess env.

Running coding-agent CLIs inside the workspace happens via the public
`run_coding_agent_cli` method; consumers don't see the working_dir path.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import tempfile
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.primitives import PluginMeta
from app.core.workspace import (
    CodingAgentCliResult,
    HealthStatus,
    OnStreamLine,
    WorkspaceExecError,
    WorkspaceProvisionError,
    WorkspaceSpec,
    register_workspace_provider,
)
from app.domain import vcs

log = structlog.get_logger("in_process_workspace")


_ASKPASS_CONTENT = """#!/bin/sh
# yaaos: emits $YAAOS_GIT_TOKEN for git to read. Created per-workspace,
# unlinked after the auth-needing git command completes.
exec printf '%s\\n' "$YAAOS_GIT_TOKEN"
"""


async def _stream_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    stdin: bytes | None,
    on_stream_line: OnStreamLine,
) -> tuple[bytes, bytes]:
    """Consume stdout line-by-line, forward each line to `on_stream_line`,
    and return `(stdout_buffer, stderr_buffer)` once the process exits.

    Stdin (if any) is written once up front and stdin is closed. stderr is
    buffered with `proc.stderr.read()` so we don't deadlock on a stderr-heavy
    child while we're servicing stdout. Callback exceptions are logged but
    don't abort the stream — losing one event shouldn't fail the whole run.
    """
    if stdin is not None and proc.stdin is not None:
        proc.stdin.write(stdin)
        await proc.stdin.drain()
        proc.stdin.close()

    stdout_chunks: list[bytes] = []

    async def _consume_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            stdout_chunks.append(line)
            try:
                await on_stream_line(line)
            except Exception:
                log.exception("workspace.in_process.on_stream_line_failed")

    async def _consume_stderr() -> bytes:
        assert proc.stderr is not None
        return await proc.stderr.read()

    _, stderr_b = await asyncio.gather(_consume_stdout(), _consume_stderr())
    await proc.wait()
    return b"".join(stdout_chunks), stderr_b


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Best-effort SIGTERM → 2s grace → SIGKILL of the subprocess's group.

    `start_new_session=True` at spawn time put the subprocess in its own
    group, so `killpg` reaches every descendant (the Claude Code CLI shells
    out to its own children — `git`, tool subprocesses — and we want them
    all reaped). Used from both the timeout path and the caller-cancel path.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.sleep(2)
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class InProcessWorkspaceProvider:
    meta = PluginMeta(
        id="in_process",
        type="workspace",
        display_name="In-Process Workspace",
        description="Host tempdir + git clone. POC only — no isolation; M02+ adds Docker workspaces.",
    )

    async def provision(self, spec: WorkspaceSpec) -> dict[str, Any]:
        """Create a tempdir + git clone the repo at spec.sha.

        Token handling:
          - Issued fresh by `vcs.get_installation_token(plugin_id, org_id)` immediately
            before clone, used once, then forgotten.
          - Passed to git via GIT_ASKPASS (script reads it from env, never argv).
          - Askpass script is created in a sibling temp location (NOT inside
            working_dir, since git's clone needs the target dir to be empty),
            unlinked in `finally`.
        """
        if spec.org_id is None:
            raise WorkspaceProvisionError("WorkspaceSpec.org_id required for git clone")
        working_dir = tempfile.mkdtemp(prefix="yaaos-ws-")
        askpass_path: str | None = None
        try:
            askpass_path = self._write_askpass()
            token = await vcs.get_installation_token(spec.repo.plugin_id, spec.org_id)
            clone_url = self._clone_url_for(spec.repo.plugin_id, spec.repo.external_id)
            base_env = self._git_env_with_token(askpass_path, token)

            # Step 1: clone the branch (or default branch) shallowly.
            branch = spec.branch_name or "HEAD"
            await self._run_subprocess(
                ["git", "clone", "--depth=1", "--branch", branch, clone_url, working_dir],
                env=base_env,
                timeout_seconds=300,
            )

            # Step 2: if sha != HEAD of the cloned branch, fetch+checkout it.
            # (Branch may have advanced between PR creation and now; agents must
            # see exactly the PR's head sha.)
            if spec.sha and spec.sha != "HEAD":
                await self._run_subprocess(
                    ["git", "-C", working_dir, "fetch", "--depth=1", "origin", spec.sha],
                    env=base_env,
                    timeout_seconds=120,
                )
                await self._run_subprocess(
                    ["git", "-C", working_dir, "checkout", spec.sha],
                    env={**os.environ},  # local checkout, no auth needed
                    timeout_seconds=30,
                )

            # Step 3: fetch the PR's base commit as an orphan in the shallow
            # store so the agent can run `git diff <base_sha>..HEAD` without us
            # inlining the diff into its prompt. One extra round-trip, one
            # object — provisioning stays under a few seconds. The fetch is
            # best-effort: if it fails (e.g. base_sha unreachable), we log and
            # proceed; the agent loses git-diff capability but reviewing the
            # head checkout still works.
            if spec.base_sha:
                try:
                    await self._run_subprocess(
                        ["git", "-C", working_dir, "fetch", "--depth=1", "origin", spec.base_sha],
                        env=base_env,
                        timeout_seconds=120,
                    )
                except WorkspaceProvisionError as e:
                    log.warning(
                        "workspace.in_process.base_sha_fetch_failed",
                        base_sha=spec.base_sha,
                        error=str(e),
                    )

            # Marker for debugging / human inspection.
            try:
                with open(os.path.join(working_dir, ".yaaos-workspace"), "w", encoding="utf-8") as f:
                    f.write(
                        f"plugin_id={spec.repo.plugin_id}\nrepo={spec.repo.external_id}\nsha={spec.sha}\n"
                    )
            except OSError:
                # marker is best-effort; don't fail provision over it
                pass
        except WorkspaceProvisionError:
            shutil.rmtree(working_dir, ignore_errors=True)
            raise
        except Exception as e:
            shutil.rmtree(working_dir, ignore_errors=True)
            raise WorkspaceProvisionError(f"in_process_workspace.provision failed: {e}") from e
        finally:
            if askpass_path:
                try:
                    os.unlink(askpass_path)
                except OSError:
                    pass

        log.info(
            "workspace.in_process.provisioned",
            working_dir=working_dir,
            repo=spec.repo.external_id,
            sha=spec.sha,
        )
        return {"working_dir": working_dir}

    async def run_coding_agent_cli(
        self,
        plugin_state: dict[str, Any],
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
        on_stream_line: OnStreamLine | None = None,
    ) -> CodingAgentCliResult:
        """Run a coding-agent CLI inside the workspace.

        cwd is the workspace's internal tempdir (private to this plugin).
        Two kill paths share the same SIGTERM → 2s grace → SIGKILL escalation
        against the process group (so the CLI's child processes are reaped too):

        - **Timeout** (`asyncio.wait_for(..., timeout=timeout_seconds)`) — returns
          a `CodingAgentCliResult(timed_out=True, exit_code=-1, ...)`.
        - **Cancellation** — caller (e.g. `reviewer.cancel_pending`) cancels the
          surrounding task. We kill the process group, then re-raise
          `CancelledError` so the cancellation unwinds normally.

        When `on_stream_line` is provided, stdout is consumed line-by-line and
        each newline-terminated chunk is forwarded to the callback as it
        arrives. The accumulated stdout is still returned in the result for
        callers that want a final blob. When `on_stream_line` is None, the
        buffered (`communicate`) path runs — semantically identical to before.
        """
        working_dir = plugin_state.get("working_dir")
        if not working_dir or not os.path.isdir(working_dir):
            raise WorkspaceExecError(
                f"in_process_workspace state missing working_dir or dir gone: {working_dir!r}"
            )

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=working_dir,
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # so we can SIGKILL the process group
            )
        except (FileNotFoundError, OSError) as e:
            raise WorkspaceExecError(f"could not spawn {argv[0]}: {e}") from e

        timed_out = False
        stdout_b: bytes = b""
        stderr_b: bytes = b""
        try:
            if on_stream_line is not None:
                # Streaming path: read stdout line-by-line, forward each chunk
                # to the callback. stderr stays buffered (only consulted on
                # failure). stdin is written once up front (callers using the
                # streaming path don't pipe interactive input).
                stdout_b, stderr_b = await asyncio.wait_for(
                    _stream_subprocess(proc, stdin=stdin, on_stream_line=on_stream_line),
                    timeout=timeout_seconds,
                )
            else:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=stdin),
                    timeout=timeout_seconds,
                )
        except TimeoutError:
            timed_out = True
            await _kill_process_group(proc)
            try:
                drained_out, drained_err = await proc.communicate()
                stdout_b = stdout_b + drained_out
                stderr_b = stderr_b + drained_err
            except Exception:
                pass
        except asyncio.CancelledError:
            # Caller-initiated cancel — kill the subprocess group, drain pipes
            # (with a short timeout so a stuck child doesn't block the cancel
            # forever), then propagate the cancellation. The drain runs inside
            # the except block so a subsequent re-cancel doesn't fire here.
            await _kill_process_group(proc)
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except (TimeoutError, Exception):
                pass
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else -1
        return CodingAgentCliResult(
            exit_code=exit_code,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration_ms=duration_ms,
        )

    async def destroy(self, plugin_state: dict[str, Any]) -> None:
        working_dir = plugin_state.get("working_dir")
        if not working_dir:
            return
        if not os.path.isdir(working_dir):
            return
        shutil.rmtree(working_dir, ignore_errors=True)
        log.info("workspace.in_process.destroyed", working_dir=working_dir)

    async def health_check(self) -> HealthStatus:
        # tempdir is always available in M01.
        return HealthStatus(healthy=True, message="ok", checked_at=datetime.now(UTC))

    # ── private helpers ───────────────────────────────────────────────────

    @staticmethod
    def _clone_url_for(plugin_id: str, external_id: str) -> str:
        """Build the HTTPS clone URL for the given VCS plugin + repo identifier.

        For GitHub: `https://github.com/<owner>/<repo>.git` (external_id is `<owner>/<repo>`).
        Future plugins (gitlab, etc.) would extend this.
        """
        if plugin_id == "github":
            return f"https://github.com/{external_id}.git"
        raise WorkspaceProvisionError(
            f"in_process_workspace: no clone URL pattern for plugin_id={plugin_id!r}"
        )

    @staticmethod
    def _write_askpass() -> str:
        """Write the GIT_ASKPASS script to a unique temp file, chmod 0700.

        Lives outside any workspace working_dir so git clone (which requires
        an empty target) can't conflict with it.
        """
        fd, path = tempfile.mkstemp(prefix="yaaos-askpass-", suffix=".sh")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_ASKPASS_CONTENT)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    @staticmethod
    def _git_env_with_token(askpass_path: str, token: str) -> dict[str, str]:
        return {
            **os.environ,
            "GIT_ASKPASS": askpass_path,
            "GIT_TERMINAL_PROMPT": "0",
            "YAAOS_GIT_TOKEN": token,
        }

    @staticmethod
    async def _run_subprocess(
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        """Run a setup-time subprocess (e.g., git). Raises WorkspaceProvisionError
        on non-zero exit or timeout. Internal — NOT exposed on the Protocol."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as e:
            raise WorkspaceProvisionError(f"could not spawn {argv[0]}: {e}") from e

        try:
            _stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError as e:
            await _kill_process_group(proc)
            raise WorkspaceProvisionError(f"{argv[0]} timed out after {timeout_seconds}s") from e

        if proc.returncode != 0:
            stderr_text = stderr_b.decode("utf-8", errors="replace").strip()
            raise WorkspaceProvisionError(
                f"{argv[0]} exited {proc.returncode}: {stderr_text or '(no stderr)'}"
            )


_provider = InProcessWorkspaceProvider()


def bootstrap() -> None:
    """Register the provider. Called at import time from __init__."""
    register_workspace_provider(_provider)


def get_provider() -> InProcessWorkspaceProvider:
    return _provider
