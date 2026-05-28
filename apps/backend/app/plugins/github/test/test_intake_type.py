"""GitHub intake type — event publishing contract.

The HTTP boundary, signature verification, and install-binding lookups are
covered by sibling test files. Here we drive `_prepare_pr_review` directly
so the assertion stays focused on the SSE-bus side effect.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from uuid import uuid4

import pytest

from app.core.events import Event, EventFilter, subscribe
from app.core.workflow import (
    CommandCategory,
    CommandContext,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    scoped_engine,
)
from app.plugins.github.intake_type import GithubIntakeType


class _NoopLocal:
    kind: Literal["Noop"] = "Noop"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


@pytest.fixture
def _stub_pr_review_engine():  # type: ignore[no-untyped-def]
    """Register a one-step `pr_review_v1` workflow so `_prepare_pr_review`'s
    `engine.start(workflow_name="pr_review_v1", ...)` resolves without
    pulling in the full reviewer command set."""
    with scoped_engine() as eng:
        eng.register_command(_NoopLocal())
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(
                    Step(
                        id="only",
                        command_kind="Noop",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="only",
            )
        )
        yield eng


def _pr_opened_payload() -> dict:
    """Minimal `pull_request.opened` body — enough for `_parse_pr` plus the
    fork / bot filters that `_prepare_pr_review` runs before insert."""
    pr = {
        "number": 7,
        "title": "T",
        "body": "B",
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": "https://github.com/acme/web/pull/7",
        "user": {"login": "alice", "type": "User"},
        "head": {
            "ref": "feat",
            "sha": "ccc",
            "repo": {"fork": False, "full_name": "acme/web"},
        },
        "base": {
            "ref": "main",
            "sha": "aaa",
            "repo": {"full_name": "acme/web"},
        },
        "created_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-01T10:00:00Z",
        "labels": [],
    }
    return {"action": "opened", "pull_request": pr, "repository": {"full_name": "acme/web"}}


@pytest.mark.asyncio
async def test_prepare_pr_review_publishes_ticket_status_changed(db_session, _stub_pr_review_engine) -> None:
    """A GitHub PR-opened webhook must broadcast `TicketStatusChanged` so
    the SSE subscriber invalidates the tickets list query. Without this,
    the new PR row stays invisible in the SPA until a hard refresh."""
    seen: list[Event] = []

    async def consume() -> None:
        async for ev in subscribe(EventFilter(kinds=["ticket_status_changed"])):
            seen.append(ev)
            return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    org_id = uuid4()
    outcome = await GithubIntakeType()._prepare_pr_review(
        payload=_pr_opened_payload(),
        delivery="evt-test-1",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    assert outcome.detail == "pr_review_started"

    await asyncio.wait_for(consumer, timeout=1.0)
    assert len(seen) == 1
    evt = seen[0]
    assert evt.kind == "ticket_status_changed"
    assert evt.previous_status is None  # type: ignore[attr-defined]
    assert evt.new_status == "running"  # type: ignore[attr-defined]
    assert evt.repo_external_id == "acme/web"  # type: ignore[attr-defined]
