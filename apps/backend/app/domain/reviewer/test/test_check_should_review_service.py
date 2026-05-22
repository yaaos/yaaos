"""`CheckShouldReview` — admission gate against the ticket payload.

Service test: real DB, real ticket, the command opens its own session via
`db_session()`. Covers each skip reason (draft / fork / labels / bot author)
and the happy path that proceeds to provisioning.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.core.workflow import CommandContext
from app.domain.reviewer.commands import (
    SKIP_LABELS,
    CheckShouldReview,
    _decide_skip,
)
from app.domain.tickets import create as create_ticket

# ── Pure-function checks on _decide_skip ────────────────────────────────


def test_decide_skip_draft() -> None:
    assert _decide_skip({"is_draft": True}) == "draft"


def test_decide_skip_fork() -> None:
    assert _decide_skip({"is_draft": False, "is_fork": True}) == "fork"


def test_decide_skip_label_match() -> None:
    payload = {"is_draft": False, "is_fork": False, "labels": ["WIP", "needs-review"]}
    assert _decide_skip(payload) == "label:wip"


def test_decide_skip_bot_author() -> None:
    payload = {"is_draft": False, "is_fork": False, "labels": [], "author_login": "dependabot[bot]"}
    assert _decide_skip(payload) == "bot_author"


def test_decide_skip_happy_path_returns_none() -> None:
    payload = {
        "is_draft": False,
        "is_fork": False,
        "labels": ["enhancement"],
        "author_login": "alice",
    }
    assert _decide_skip(payload) is None


def test_skip_labels_are_lowercase_case_insensitive_match() -> None:
    """Sanity check on the constant — comparison happens after lowercasing
    both sides, so accidental case-mismatch in `SKIP_LABELS` would silently
    fail to match."""
    for label in SKIP_LABELS:
        assert label == label.lower()


# ── Service tests — real DB, full `execute()` path ──────────────────────


async def _create_ticket(*, payload: dict[str, Any], db_session) -> UUID:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    ticket_id, _ = await create_ticket(
        type="github_pr",
        payload=payload,
        idempotency_key=f"key-{uuid4()}",
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id="123",
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    await db_session.commit()
    return ticket_id


def _ctx(ticket_id: UUID) -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(ticket_id),
        step_id="check",
        attempt=0,
    )


async def test_skip_when_draft(db_session) -> None:  # type: ignore[no-untyped-def]
    ticket_id = await _create_ticket(payload={"is_draft": True, "is_fork": False}, db_session=db_session)
    outcome = await CheckShouldReview().execute({}, _ctx(ticket_id))
    assert outcome.label == "skip"
    assert outcome.outputs["reason"] == "draft"


async def test_skip_when_fork(db_session) -> None:  # type: ignore[no-untyped-def]
    ticket_id = await _create_ticket(payload={"is_draft": False, "is_fork": True}, db_session=db_session)
    outcome = await CheckShouldReview().execute({}, _ctx(ticket_id))
    assert outcome.label == "skip"
    assert outcome.outputs["reason"] == "fork"


async def test_skip_when_skip_label_present(db_session) -> None:  # type: ignore[no-untyped-def]
    ticket_id = await _create_ticket(
        payload={"is_draft": False, "is_fork": False, "labels": ["WIP"]},
        db_session=db_session,
    )
    outcome = await CheckShouldReview().execute({}, _ctx(ticket_id))
    assert outcome.label == "skip"
    assert outcome.outputs["reason"].startswith("label:")


async def test_skip_when_bot_author(db_session) -> None:  # type: ignore[no-untyped-def]
    ticket_id = await _create_ticket(
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": [],
            "author_login": "dependabot[bot]",
        },
        db_session=db_session,
    )
    outcome = await CheckShouldReview().execute({}, _ctx(ticket_id))
    assert outcome.label == "skip"
    assert outcome.outputs["reason"] == "bot_author"


async def test_happy_path_returns_success_with_pr_external_id(db_session) -> None:  # type: ignore[no-untyped-def]
    ticket_id = await _create_ticket(
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": ["enhancement"],
            "author_login": "alice",
            "pr_external_id": "42",
        },
        db_session=db_session,
    )
    outcome = await CheckShouldReview().execute({}, _ctx(ticket_id))
    assert outcome.label == "success"
    assert outcome.outputs["pr_external_id"] == "42"
