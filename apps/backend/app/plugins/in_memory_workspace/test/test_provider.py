"""Tests for the in-process workspace provider.

The real `provision()` does `git clone` via the VCS plugin's installation
token. These tests inject a fake VCS plugin (no real GitHub call) and use a
local bare git repo as the clone source — so the full clone code path runs
end-to-end without network.

Tests for `run_coding_agent_cli` use trivial subprocesses (`/bin/sh -c`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any
from uuid import uuid4

import pytest

from app.core.plugin_kit import PluginMeta
from app.core.workspace import RepoRefForSpec, WorkspaceProvisionError, WorkspaceSpec
from app.plugins.in_memory_workspace import get_provider
from app.testing.isolation import scoped_vcs_plugin


class _FakeGitHubPlugin:
    """Minimal VCSPlugin satisfying just what in_memory_workspace needs."""

    meta = PluginMeta(id="github", type="vcs", display_name="GitHub (fake)")

    def __init__(self, token: str = "fake-token-abc") -> None:
        self._token = token
        # Tests overwrite this with a `file://` URL pointing at a local bare
        # repo so the real `git clone` path runs without hitting github.com.
        self.clone_url_fn = lambda external_id: f"https://github.com/{external_id}.git"

    async def get_installation_token(self, org_id: Any) -> str:
        del org_id
        return self._token

    def clone_url(self, repo_external_id: str) -> str:
        return self.clone_url_fn(repo_external_id)

    # The remaining VCSPlugin methods aren't reached by these tests.


def _make_bare_repo_with_commit() -> tuple[str, str, str]:
    """Create a local bare git repo with one commit on `main`. Returns
    (bare_repo_path, clone_url, head_sha)."""
    bare_dir = tempfile.mkdtemp(prefix="yaaos-test-bare-")
    work_dir = tempfile.mkdtemp(prefix="yaaos-test-work-")
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", bare_dir], check=True)
    subprocess.run(["git", "init", "--initial-branch=main", work_dir], check=True)
    subprocess.run(["git", "-C", work_dir, "config", "user.email", "test@yaaos.local"], check=True)
    subprocess.run(["git", "-C", work_dir, "config", "user.name", "yaaos-test"], check=True)
    with open(os.path.join(work_dir, "README.md"), "w") as f:
        f.write("hello yaaos\n")
    subprocess.run(["git", "-C", work_dir, "add", "."], check=True)
    subprocess.run(["git", "-C", work_dir, "commit", "-m", "initial"], check=True)
    subprocess.run(["git", "-C", work_dir, "remote", "add", "origin", bare_dir], check=True)
    subprocess.run(["git", "-C", work_dir, "push", "origin", "main"], check=True)
    sha = subprocess.run(
        ["git", "-C", work_dir, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return bare_dir, f"file://{bare_dir}", sha


@pytest.fixture(autouse=True)
def _register_fake_github(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Register a fake github vcs plugin for the duration of one test."""
    del monkeypatch
    fake = _FakeGitHubPlugin()
    with scoped_vcs_plugin(fake):  # type: ignore[arg-type]
        yield fake


@pytest.mark.asyncio
async def test_provision_clones_repo_at_sha(_register_fake_github) -> None:
    provider = get_provider()
    bare_path, clone_url, sha = _make_bare_repo_with_commit()

    # Redirect the fake plugin's clone URL at our local bare repo so the real
    # `git clone` runs without network.
    _register_fake_github.clone_url_fn = lambda _external_id: clone_url

    state = await provider.provision(
        WorkspaceSpec(
            repo=RepoRefForSpec(plugin_id="github", external_id="acme/web"),
            sha=sha,
            branch_name="main",
            org_id=uuid4(),
        )
    )
    try:
        working_dir = state["working_dir"]
        assert os.path.isdir(working_dir)
        # Clone happened: README is present, .git exists.
        assert os.path.isfile(os.path.join(working_dir, "README.md"))
        assert os.path.isdir(os.path.join(working_dir, ".git"))
        # Marker is written.
        assert os.path.isfile(os.path.join(working_dir, ".yaaos-workspace"))
        # HEAD matches the requested sha.
        head = subprocess.run(
            ["git", "-C", working_dir, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == sha
    finally:
        await provider.destroy(state)
        shutil.rmtree(bare_path, ignore_errors=True)


@pytest.mark.asyncio
async def test_provision_requires_org_id() -> None:
    provider = get_provider()
    with pytest.raises(WorkspaceProvisionError, match="org_id"):
        await provider.provision(
            WorkspaceSpec(
                repo=RepoRefForSpec(plugin_id="github", external_id="acme/web"),
                sha="abc123",
                # org_id intentionally omitted
            )
        )


@pytest.mark.asyncio
async def test_destroy_is_idempotent() -> None:
    provider = get_provider()
    await provider.destroy({"working_dir": "/tmp/this-path-does-not-exist"})
    await provider.destroy({})  # missing key — no error


@pytest.mark.asyncio
async def test_health_check() -> None:
    h = await get_provider().health_check()
    assert h.healthy is True


@pytest.mark.asyncio
async def test_run_coding_agent_cli_echoes_stdout() -> None:
    provider = get_provider()
    working_dir = tempfile.mkdtemp(prefix="yaaos-ws-test-")
    try:
        result = await provider.run_coding_agent_cli(
            {"working_dir": working_dir},
            argv=["/bin/sh", "-c", "echo hello && echo err >&2"],
            timeout_seconds=5,
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == "hello"
        assert result.stderr.strip() == "err"
        assert result.timed_out is False
        assert result.duration_ms >= 0
    finally:
        shutil.rmtree(working_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_run_coding_agent_cli_passes_stdin() -> None:
    provider = get_provider()
    working_dir = tempfile.mkdtemp(prefix="yaaos-ws-test-")
    try:
        result = await provider.run_coding_agent_cli(
            {"working_dir": working_dir},
            argv=["/bin/cat"],
            stdin=b"piped-content",
            timeout_seconds=5,
        )
        assert result.exit_code == 0
        assert result.stdout == "piped-content"
    finally:
        shutil.rmtree(working_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_run_coding_agent_cli_timeout_marks_timed_out() -> None:
    provider = get_provider()
    working_dir = tempfile.mkdtemp(prefix="yaaos-ws-test-")
    try:
        result = await provider.run_coding_agent_cli(
            {"working_dir": working_dir},
            argv=["/bin/sleep", "10"],
            timeout_seconds=1,
        )
        assert result.timed_out is True
        # exit_code is whatever signal killed it; we don't assert on the value
        # (varies across platforms), only the timed_out flag.
    finally:
        shutil.rmtree(working_dir, ignore_errors=True)
