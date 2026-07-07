"""Fixture for integration tests that round-trip against a live `apps/fake-github`
subprocess rather than `httpx_mock`.

`apps/fake-github` is a peer service with its own `pyproject.toml`/venv — its
top-level package is also named `app`, so importing it in-process would
collide with the backend's own `app` package. Spawning it as a real subprocess
(via its own uv-managed `.venv`) sidesteps that and matches how the e2e stack
runs it: real HTTP round trips, real git smart-HTTP protocol for push.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


def _fake_github_dir() -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if parent.name == "backend":
            return parent.parent / "fake-github"
    raise RuntimeError("could not locate the apps/backend ancestor of this test file")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_github_base_url(tmp_path: Path) -> Iterator[str]:
    """Spawn a live `apps/fake-github` uvicorn subprocess on an ephemeral port,
    backed by a fresh temp dir for its bare git repos. Yields the base URL;
    terminates the subprocess on teardown.

    Skips (rather than fails) when `apps/fake-github/.venv` hasn't been
    created yet — `cd apps/fake-github && uv sync` provisions it; `bin/ci`
    does this as part of its own setup surface for this test.
    """
    fake_github_dir = _fake_github_dir()
    venv_python = fake_github_dir / ".venv" / "bin" / "python"
    if not venv_python.exists():
        pytest.skip(f"{venv_python} missing — run `uv sync` in apps/fake-github first")

    port = _free_port()
    repos_dir = tmp_path / "fake-github-repos"
    repos_dir.mkdir()
    env = {
        **os.environ,
        "FAKE_GITHUB_REPOS_DIR": str(repos_dir),
        "GITHUB_WEBHOOK_SECRET": "TEST-FAKE-NOT-FOR-PROD-aaaaaaaaaaaaaaaa",
    }
    proc = subprocess.Popen(
        [str(venv_python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(fake_github_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout else ""
                raise RuntimeError(f"fake-github exited early:\n{out}")
            try:
                resp = httpx.get(f"{base_url}/__test/posted_comments", timeout=0.5)
                if resp.status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        if not healthy:
            raise RuntimeError("fake-github did not become healthy in time")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
