"""Service test: two concurrent `start_incremental_review` calls for the same PR
produce at most one new ReviewRow, with exactly one `engine.start` dispatched.

Uses `asyncio.gather` to race two calls under the per-PR advisory lock. The
lock-inside-check added to `_create_incremental_review` ensures that whichever
call loses the lock race finds the winner's row in-flight and returns
`"skipped:in_flight"` rather than inserting a second ReviewRow.

Seeds via `get_sessionmaker()` (independent committed sessions) so the two
concurrent `start_incremental_review` calls — which each open their own DB
sessions — truly race on Postgres. The `db_session` rollback fixture is
deliberately NOT used here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.database import get_sessionmaker
from app.core.vcs import VCSPullRequest
from app.core.workflow import WorkflowEngine, bind_engine
from app.domain.reviewer.incremental_trigger import start_incremental_review
from app.domain.reviewer.models import ReviewRow
from app.domain.tickets import attach_pr_to_ticket
from app.domain.tickets import create as create_ticket
from app.domain.tickets import upsert as upsert_pr
from app.testing.seed import delete_pull_request, delete_ticket
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.asyncio]

_REPO_EXTERNAL_ID = "owner/repo"
_PR_EXTERNAL_ID = "owner/repo#99"
_PREV_SHA = "aaaa1111"
_HEAD_SHA = "bbbb2222"
_PLUGIN_ID = "github"


class _RecordingEngine(WorkflowEngine):
    """WorkflowEngine subclass that records `start` calls without executing them.

    Returns a fixed UUID so the caller can commit and proceed. Does not
    validate workflow registration — callers that just want to count dispatches
    do not need to register any workflow.
    """

    def __init__(self) -> None:
        super().__init__()
        self.start_calls: list[dict] = []

    async def start(self, *, workflow_name: str, ticket_id: str, **kwargs) -> str:  # type: ignore[override]
        self.start_calls.append({"workflow_name": workflow_name, "ticket_id": ticket_id})
        return str(uuid4())


@dataclass
class _Seeded:
    """Tracks committed rows for teardown cleanup."""

    pr_id: UUID | None = None
    ticket_id: UUID | None = None
    review_ids: list[UUID] = field(default_factory=list)


async def _clean(seeded: _Seeded) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        if seeded.review_ids:
            await s.execute(delete(ReviewRow).where(ReviewRow.id.in_(seeded.review_ids)))
        await s.commit()

    # pull_requests.ticket_id → tickets.id (NOT NULL), so delete PR before ticket.
    async with sessionmaker() as s:
        if seeded.pr_id is not None:
            await delete_pull_request(seeded.pr_id, session=s)
        await s.commit()
    async with sessionmaker() as s:
        if seeded.ticket_id is not None:
            await delete_ticket(seeded.ticket_id, session=s)
        await s.commit()


@pytest_asyncio.fixture
async def _seeded() -> AsyncIterator[_Seeded]:
    seeded = _Seeded()
    yield seeded
    await _clean(seeded)


async def _seed_pr_and_ticket(org_id: UUID, seeded: _Seeded) -> UUID:
    """Create a PR + linked ticket. Returns the PR id."""
    sessionmaker = get_sessionmaker()
    ext_id = f"ir-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    vcs_pr = VCSPullRequest(
        plugin_id=_PLUGIN_ID,
        external_id=_PR_EXTERNAL_ID,
        repo_external_id=_REPO_EXTERNAL_ID,
        number=99,
        title="concurrent push test PR",
        body=None,
        author_login="dev",
        author_type="user",
        base_branch="main",
        head_branch="feature",
        base_sha=_PREV_SHA,
        head_sha=_HEAD_SHA,
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="https://github.com/owner/repo/pull/99",
        created_at=now,
        updated_at=now,
    )
    async with sessionmaker() as s:
        ticket_id, _ = await create_ticket(
            type="github_pr",
            payload={"is_draft": False, "is_fork": False},
            idempotency_key=ext_id,
            org_id=org_id,
            title="concurrent push test",
            plugin_id=_PLUGIN_ID,
            repo_external_id=_REPO_EXTERNAL_ID,
            session=s,
        )
        pr = await upsert_pr(vcs_pr, ticket_id=ticket_id, org_id=org_id, session=s)
        pr_id = pr.id
        await attach_pr_to_ticket(ticket_id, pr_id=pr_id, session=s)
        await s.commit()

    seeded.pr_id = pr_id
    seeded.ticket_id = ticket_id
    return pr_id


@pytest.mark.service
async def test_concurrent_pushes_produce_exactly_one_review_row(
    _migrated_schema: None,
    _seeded: _Seeded,
) -> None:
    """Two concurrent `start_incremental_review` calls for the same PR with no
    prior review produce exactly one ReviewRow. The losing call returns
    `"skipped:in_flight"`, the winning row has `pending_replay=True`, and
    exactly one `engine.start` is dispatched.
    """
    org_id = uuid4()
    pr_id = await _seed_pr_and_ticket(org_id, _seeded)

    engine = _RecordingEngine()
    prior_engine = bind_engine(engine)
    try:
        with register_stub_vcs(plugin_id=_PLUGIN_ID) as stub:
            # detect_force_push returns False by default → not a force push → is ancestor.
            # No commit messages configured → no base-merge signal.
            stub.set_force_push(_REPO_EXTERNAL_ID, _PREV_SHA, _HEAD_SHA, False)

            results = await asyncio.gather(
                start_incremental_review(
                    pr_id,
                    new_head_sha=_HEAD_SHA,
                    prev_head_sha=_PREV_SHA,
                    org_id=org_id,
                ),
                start_incremental_review(
                    pr_id,
                    new_head_sha=_HEAD_SHA,
                    prev_head_sha=_PREV_SHA,
                    org_id=org_id,
                ),
            )
    finally:
        bind_engine(prior_engine)

    scheduled_results = [r for r in results if r == "scheduled"]
    skipped_results = [r for r in results if r == "skipped:in_flight"]

    assert len(scheduled_results) == 1, f"Expected exactly 1 'scheduled' result; got {results!r}"
    assert len(skipped_results) == 1, f"Expected exactly 1 'skipped:in_flight' result; got {results!r}"

    # Exactly one ReviewRow exists for this PR.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        rows = (await s.execute(select(ReviewRow).where(ReviewRow.pr_id == pr_id))).scalars().all()

    assert len(rows) == 1, f"Expected exactly 1 ReviewRow; found {len(rows)}"
    surviving_row = rows[0]
    _seeded.review_ids.append(surviving_row.id)

    assert surviving_row.pending_replay is True, (
        "Surviving ReviewRow must have pending_replay=True (second push signal)"
    )

    # Exactly one engine.start was dispatched.
    assert len(engine.start_calls) == 1, f"Expected exactly 1 engine.start call; got {engine.start_calls!r}"
    assert engine.start_calls[0]["workflow_name"] == "incremental_review_v1"
