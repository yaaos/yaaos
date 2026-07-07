"""Integration: `core/vcs` write-surface dispatch wrappers round-tripped
against a live `apps/fake-github` subprocess (see `conftest.py`).

Drives `vcs.create_pr`/`vcs.approve_pr`/`vcs.has_active_approval`/
`vcs.resolve_finding_thread` — the module-level dispatch wrappers, not the
`GitHubPlugin` class directly — so the span-wrapped path every real caller
uses is what's under test. A separate test proves `git push` acceptance
against the fake's clone URL over the git smart-HTTP protocol.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import get_settings
from app.core.vcs import approve_pr, create_pr, has_active_approval, post_finding, resolve_finding_thread
from app.domain.orgs import create_org
from app.plugins.github import GitHubPlugin, record_app_install


@pytest.fixture
async def github_org(
    db_session, monkeypatch: pytest.MonkeyPatch, fake_github_base_url: str
) -> AsyncIterator[UUID]:
    """An org with an active GitHub App installation, with the registered
    `GitHubPlugin` singleton pointed at the live fake-github subprocess."""
    org = await create_org(db_session, slug="vcs-write-surface-org", display_name="VCS Write Surface Org")
    await record_app_install(
        db_session,
        org_id=org.id,
        install_external_id="install-fake-1",
        account_login="acme",
    )
    await db_session.commit()

    monkeypatch.setenv("YAAOS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("YAAOS_GITHUB_APP_PRIVATE_KEY", "fake-pem-not-a-real-key")
    monkeypatch.setenv("YAAOS_GITHUB_APP_WEBHOOK_SECRET", "whsec-test")
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos-test")
    get_settings.cache_clear()
    monkeypatch.setattr(GitHubPlugin, "base_url", fake_github_base_url)

    yield org.id


async def test_pr_write_surface_round_trip_against_fake_github(github_org: UUID) -> None:
    """create_pr -> has_active_approval(False) -> approve_pr ->
    has_active_approval(True) -> resolve_finding_thread, all against the live
    fake. Also proves create_pr's idempotency: a second create for the same
    head branch returns the same PR instead of erroring.
    """
    org_id = github_org
    repo_external_id = "acme/review-happy"

    external_id = await create_pr(
        "github",
        org_id,
        repo_external_id,
        head_branch="yaaos/write-surface-test",
        base_branch="main",
        title="Write-surface round trip",
        body="Opened by the core/vcs integration test.",
    )
    assert external_id.startswith("acme/review-happy#")

    external_id_again = await create_pr(
        "github",
        org_id,
        repo_external_id,
        head_branch="yaaos/write-surface-test",
        base_branch="main",
        title="Write-surface round trip (again)",
        body="Second attempt — must resolve to the same PR.",
    )
    assert external_id_again == external_id

    assert await has_active_approval("github", org_id, external_id) is False

    await approve_pr("github", org_id, external_id)

    assert await has_active_approval("github", org_id, external_id) is True

    comment_external_id = await post_finding(
        "github",
        org_id,
        external_id,
        file="src/example.py",
        line_start=10,
        line_end=10,
        severity="blocker",
        category="security",
        confidence="verified",
        finding_display_id=1,
        rationale="Test finding for the write-surface round trip.",
        rule_violated="test-rule",
        rule_source="house",
        suggested_fix=None,
    )

    # No exception == success; GitHub's GraphQL mutation returns no durable
    # state this test can observe from the REST surface fake-github mirrors.
    await resolve_finding_thread("github", org_id, external_id, comment_external_id)


async def test_git_push_to_fake_github_clone_url_succeeds(fake_github_base_url: str) -> None:
    """`git push` over HTTP to the fake's clone URL succeeds — the mechanism
    the agent's `git push origin HEAD` and the `PushBranch` command rely on.
    """
    clone_url = f"{fake_github_base_url}/acme/review-happy.git"
    env = {
        "GIT_AUTHOR_NAME": "yaaos-test",
        "GIT_AUTHOR_EMAIL": "yaaos-test@example.test",
        "GIT_COMMITTER_NAME": "yaaos-test",
        "GIT_COMMITTER_EMAIL": "yaaos-test@example.test",
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
    }

    with tempfile.TemporaryDirectory() as workdir:
        _run(["git", "clone", clone_url, "checkout"], cwd=workdir, env=env)
        repo_dir = Path(workdir) / "checkout"
        _run(["git", "checkout", "-b", "yaaos/push-acceptance-test"], cwd=repo_dir, env=env)
        (repo_dir / "PUSH_TEST.md").write_text("push acceptance check\n")
        _run(["git", "add", "."], cwd=repo_dir, env=env)
        _run(["git", "commit", "-m", "push acceptance test commit"], cwd=repo_dir, env=env)

        result = subprocess.run(
            ["git", "push", "origin", "yaaos/push-acceptance-test"],
            cwd=repo_dir,
            env=env,
            capture_output=True,
        )

    assert result.returncode == 0, result.stderr.decode()


def _run(args: list[str], *, cwd: str | Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True)
