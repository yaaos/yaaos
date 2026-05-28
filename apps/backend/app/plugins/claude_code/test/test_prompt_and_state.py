"""Tests for plugin-internal prompt assembly + state computation.

Prompt assembly + state computation are plugin-internal because the
public Protocol (`review(context) -> ReviewResult`) returns vendor-neutral
`vcs.Finding`s. Tests stay close to the code they cover.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.domain.coding_agent import FindingAnchor, FindingDraft, ReviewContext
from app.domain.lessons import Lesson
from app.domain.vcs import Diff, VCSPullRequest
from app.plugins.claude_code.service import (
    _assemble_review_prompt,
    _compute_state_v2,
)


def _lesson(title: str, body: str) -> Lesson:
    now = datetime.now(UTC)
    return Lesson(
        id=uuid4(),
        org_id=uuid4(),
        plugin_id="github",
        repo_external_id="acme/web",
        title=title,
        body=body,
        source_pr_url=None,
        created_at=now,
        updated_at=now,
    )


def _ctx(**overrides) -> ReviewContext:
    now = datetime.now(UTC)
    pr = VCSPullRequest(
        plugin_id="github",
        external_id="acme/web#1",
        repo_external_id="acme/web",
        number=1,
        title="Add widget",
        body="A new widget.",
        author_login="alice",
        author_type="user",
        base_branch="main",
        head_branch="feat",
        base_sha="b",
        head_sha="h",
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="http://x",
        created_at=now,
        updated_at=now,
    )
    base = dict(
        pr=pr,
        diff=Diff(raw="diff --git a/x b/x\n+hi", files=[]),
        lessons=[],
        language_hint=None,
        prior_yaaos_comment_bodies=[],
        agent_config={},
    )
    base.update(overrides)
    return ReviewContext(**base)


def test_prompt_includes_parent_header_and_branch_refs() -> None:
    out = _assemble_review_prompt(_ctx())
    assert "yaaos parent reviewer" in out
    assert "yaaos-architecture" in out  # parent must know its subagents
    assert "Add widget" in out
    # Diff is NOT inlined — agent runs git itself. Branch refs must be present
    # so the agent knows what to diff against.
    assert "git diff" in out
    assert "## Branch" in out
    # Sanity: the raw diff body from the context isn't dumped into the
    # prompt, to keep token cost down on big PRs.
    assert "diff --git" not in out


def test_prompt_includes_language_hint_when_given() -> None:
    out = _assemble_review_prompt(_ctx(language_hint="Python"))
    assert "primarily Python" in out


def test_prompt_includes_lessons_when_given() -> None:
    out = _assemble_review_prompt(_ctx(lessons=[_lesson("watch out", "for off-by-one errors")]))
    assert "Lessons learned from past reviews" in out
    assert "watch out" in out


def test_prompt_omits_prior_yaaos_comments() -> None:
    """Full review intentionally doesn't surface prior yaaos comments to the
    agent — the aggregate's fingerprint dedup handles re-emission silently.
    Telling the agent to "not duplicate" would fight the persistence layer
    and starve the re-observation signal.
    """
    long_bodies = ["x" * 500 for _ in range(30)]
    out = _assemble_review_prompt(_ctx(prior_yaaos_comment_bodies=long_bodies))

    assert "Prior yaaos comments on this PR" not in out
    assert "- x" not in out


def _draft(severity: str) -> FindingDraft:
    return FindingDraft(
        severity=severity,  # type: ignore[arg-type]
        rule_id="r/x",
        title="t",
        body="b",
        concrete_failure_scenario="caller invokes f() without arg; raises TypeError.",
        confidence=90,
        rationale="r",
        anchor=FindingAnchor(file_path="src/foo.py", line_start=1, line_end=1),
    )


def test_compute_state_v2_approved_when_no_findings() -> None:
    assert _compute_state_v2([]) == "APPROVED"


def test_compute_state_v2_changes_requested_on_blocker() -> None:
    findings = [_draft("nit"), _draft("blocker")]
    assert _compute_state_v2(findings) == "CHANGES_REQUESTED"


def test_compute_state_v2_changes_requested_on_major() -> None:
    findings = [_draft("major")]
    assert _compute_state_v2(findings) == "CHANGES_REQUESTED"


def test_compute_state_v2_comment_for_minor_and_nit() -> None:
    findings = [_draft("minor"), _draft("nit")]
    assert _compute_state_v2(findings) == "COMMENT"
