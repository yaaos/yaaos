"""`CheckShouldReview` — admission gate with typed inputs.

Tests cover each skip reason (draft / fork / labels / bot author) and the
happy path that proceeds. All data comes from `CheckShouldReviewInputs` — no
DB lookup needed; the ticket payload was replaced by the typed TicketSnapshot
workflow input in Phase 3.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.workflow import CommandContext
from app.domain.reviewer.commands import (
    CheckShouldReview,
    CheckShouldReviewInputs,
)
from app.domain.reviewer.commands.check_should_review import (
    SKIP_LABELS,
    _decide_skip,
)

pytestmark = pytest.mark.service


# ── Pure-function checks on _decide_skip ────────────────────────────────


def test_decide_skip_draft() -> None:
    assert _decide_skip(CheckShouldReviewInputs(is_draft=True)) == "draft"


def test_decide_skip_fork() -> None:
    assert _decide_skip(CheckShouldReviewInputs(is_draft=False, is_fork=True)) == "fork"


def test_decide_skip_label_match() -> None:
    inputs = CheckShouldReviewInputs(is_draft=False, is_fork=False, labels=("WIP", "needs-review"))
    assert _decide_skip(inputs) == "label:wip"


def test_decide_skip_bot_author() -> None:
    inputs = CheckShouldReviewInputs(is_draft=False, is_fork=False, labels=(), author_login="dependabot[bot]")
    assert _decide_skip(inputs) == "bot_author"


def test_decide_skip_happy_path_returns_none() -> None:
    inputs = CheckShouldReviewInputs(
        is_draft=False,
        is_fork=False,
        labels=("enhancement",),
        author_login="alice",
    )
    assert _decide_skip(inputs) is None


def test_skip_labels_are_lowercase_case_insensitive_match() -> None:
    """Sanity check on the constant — comparison happens after lowercasing
    both sides, so accidental case-mismatch in `SKIP_LABELS` would silently
    fail to match."""
    for label in SKIP_LABELS:
        assert label == label.lower()


# ── execute() path — typed inputs, no DB ────────────────────────────────


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="check",
        attempt=0,
    )


@pytest.mark.asyncio
async def test_skip_when_draft(db_session) -> None:  # type: ignore[no-untyped-def]
    outcome = await CheckShouldReview().execute(
        CheckShouldReviewInputs(is_draft=True), _ctx(), session=db_session
    )
    assert outcome.label == "skip"
    assert outcome.outputs.skip_reason == "draft"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_skip_when_fork(db_session) -> None:  # type: ignore[no-untyped-def]
    outcome = await CheckShouldReview().execute(
        CheckShouldReviewInputs(is_draft=False, is_fork=True), _ctx(), session=db_session
    )
    assert outcome.label == "skip"
    assert outcome.outputs.skip_reason == "fork"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_skip_when_skip_label_present(db_session) -> None:  # type: ignore[no-untyped-def]
    outcome = await CheckShouldReview().execute(
        CheckShouldReviewInputs(is_draft=False, is_fork=False, labels=("WIP",)), _ctx(), session=db_session
    )
    assert outcome.label == "skip"
    assert outcome.outputs.skip_reason.startswith("label:")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_skip_when_bot_author(db_session) -> None:  # type: ignore[no-untyped-def]
    outcome = await CheckShouldReview().execute(
        CheckShouldReviewInputs(is_draft=False, is_fork=False, labels=(), author_login="dependabot[bot]"),
        _ctx(),
        session=db_session,
    )
    assert outcome.label == "skip"
    assert outcome.outputs.skip_reason == "bot_author"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_happy_path_returns_success(db_session) -> None:  # type: ignore[no-untyped-def]
    outcome = await CheckShouldReview().execute(
        CheckShouldReviewInputs(
            is_draft=False,
            is_fork=False,
            labels=("enhancement",),
            author_login="alice",
        ),
        _ctx(),
        session=db_session,
    )
    assert outcome.label == "success"
