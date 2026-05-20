"""Stub workspace provider for offline tests.

Mirrors the existing `testing/stub_coding_agent` pattern: `wrap_all_registered_workspace_providers`
walks `core.workspace._PROVIDERS` and swaps every entry for a `StubWorkspaceProvider`.

The stub:
- `provision()` creates an empty tempdir + marker file (NO git clone, no vcs lookup).
- `run_coding_agent_cli()` returns a canned empty `CodingAgentCliResult`. Tests
  using `stub_coding_agent` short-circuit before this is reached; the no-op is
  protocol-completeness only.
- `destroy()` rmtrees the tempdir.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.workspace import (
    CodingAgentCliResult,
    HealthStatus,
    WorkspaceSpec,
)

log = structlog.get_logger("testing.stub_workspace")


class StubWorkspaceProvider:
    """Wraps a real provider but skips git clone and never touches the network.

    Mirrors `plugin_id` from the wrapped provider so consumers can't tell the
    difference at the registry layer (parallel to `StubCodingAgentPlugin`).
    """

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.meta = wrapped.meta

    async def provision(self, spec: WorkspaceSpec) -> dict[str, Any]:
        working_dir = tempfile.mkdtemp(prefix="yaaos-ws-stub-")
        try:
            with open(os.path.join(working_dir, ".yaaos-workspace"), "w", encoding="utf-8") as f:
                f.write(
                    f"stub=true\n"
                    f"plugin_id={spec.repo.plugin_id}\n"
                    f"repo={spec.repo.external_id}\n"
                    f"sha={spec.sha}\n"
                )
            # The stub coding agent emits a finding anchored to
            # `src/example.ts`. Real workspaces would have cloned this
            # from the PR; the stub workspace pre-writes a placeholder so
            # the reviewer's anchor + fingerprint hashes succeed and the
            # finding isn't dropped via `findingdraft_dropped_no_file`.
            os.makedirs(os.path.join(working_dir, "src"), exist_ok=True)
            with open(os.path.join(working_dir, "src", "example.ts"), "w", encoding="utf-8") as f:
                f.write("// stub workspace placeholder for finding anchors\nexport {};\n")
        except OSError:
            pass
        return {"working_dir": working_dir}

    async def run_coding_agent_cli(
        self,
        plugin_state: dict[str, Any],
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
    ) -> CodingAgentCliResult:
        # Tests that reach this layer should be exercising coding-agent flows
        # via stub_coding_agent, which short-circuits before any workspace call.
        # This no-op preserves protocol completeness.
        del plugin_state, argv, env, stdin, timeout_seconds
        return CodingAgentCliResult(
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_ms=0,
        )

    async def read_text(self, plugin_state: dict[str, Any], path: str) -> str | None:
        working_dir = plugin_state.get("working_dir")
        if not working_dir or not os.path.isdir(working_dir):
            return None
        clean = path.lstrip("/\\")
        target = os.path.realpath(os.path.join(working_dir, clean))
        if not target.startswith(os.path.realpath(working_dir) + os.sep):
            return None
        try:
            with open(target, encoding="utf-8") as fh:
                return fh.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError, UnicodeDecodeError):
            return None

    async def write_text(self, plugin_state: dict[str, Any], path: str, content: str) -> None:
        working_dir = plugin_state.get("working_dir")
        if not working_dir or not os.path.isdir(working_dir):
            return
        clean = path.lstrip("/\\")
        target = os.path.realpath(os.path.join(working_dir, clean))
        if not target.startswith(os.path.realpath(working_dir) + os.sep):
            return
        if os.path.exists(target):
            raise RuntimeError(f"workspace file already exists: {path!r}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)

    async def destroy(self, plugin_state: dict[str, Any]) -> None:
        working_dir = plugin_state.get("working_dir")
        if working_dir and os.path.isdir(working_dir):
            shutil.rmtree(working_dir, ignore_errors=True)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="stub mode", checked_at=datetime.now(UTC))


def wrap_all_registered_workspace_providers() -> int:
    """Replace every entry in `core.workspace._PROVIDERS` with a stub wrapping it.

    Idempotent. Called from `app/main.py` when `YAAOS_WORKSPACE_STUB` is set
    (mirrors how stub_coding_agent's wrap is wired).
    """
    from app.core.workspace.service import _PROVIDERS  # noqa: PLC0415 — registry access

    count = 0
    for plugin_id, real in list(_PROVIDERS.items()):
        if isinstance(real, StubWorkspaceProvider):
            continue
        _PROVIDERS[plugin_id] = StubWorkspaceProvider(wrapped=real)
        count += 1
    log.info("stub_workspace.wrapped_all", count=count)
    return count
