"""Service tests for domain/attachments.

Covers:
- add_attachment with valid frontmatter (metadata columns populated)
- add_attachment without frontmatter (all metadata columns None)
- add_attachment with malformed frontmatter (degrades gracefully, no error)
- add_attachment over 2 MiB → AttachmentTooLargeError (413 via HTTP)
- list_attachments returns metadata newest-first
- get_attachment cross-org → AttachmentNotFoundError
- audit row `attachment.added` is written
- SSE `attachment_added` event is stashed for post-commit publication
"""

from __future__ import annotations

from textwrap import dedent
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.audit_log import Actor, list_for_entity
from app.core.auth import AuthMiddleware, Role
from app.core.identity import create_user, mint_session
from app.core.sse import GeneralEventKind
from app.domain.attachments.service import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    InvalidAttachmentFilenameError,
    TicketNotFoundError,
    add_attachment,
    get_attachment,
    list_attachments,
)
from app.domain.orgs import insert_membership, insert_org

pytestmark = pytest.mark.service


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_VALID_FRONTMATTER_BODY = dedent(
    """\
    ---
    yaaos_artifact_version: 1
    skill: pipeline-requirements
    skill_version: "1.0.0"
    artifact_type: requirements
    produced_at: "2024-01-15T10:00:00Z"
    repo_commit: abc1234
    ---
    # Requirements document body here.
    """
)

_PLAIN_BODY = "# Plain context document\n\nNo frontmatter here."

_MALFORMED_FRONTMATTER_BODY = dedent(
    """\
    ---
    yaaos_artifact_version: "not-an-int"
    skill: pipeline-requirements
    ---
    # Malformed frontmatter.
    """
)


async def _make_org_user_ticket(db_session):  # type: ignore[no-untyped-def]
    """Returns (org_id, user_id, ticket_id, actor)."""
    from app.domain.tickets import create_from_manual  # noqa: PLC0415

    user = await create_user(db_session, display_name="Tester")
    await db_session.flush()
    slug = f"att-test-{uuid4().hex[:8]}"
    org = await insert_org(db_session, slug=slug)
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="tstr")
    await db_session.flush()
    actor = Actor.user(user_id=user.id)
    ticket_id, created = await create_from_manual(
        org_id=org.org_id,
        title="Test ticket",
        repo_external_id="owner/repo",
        actor=actor,
        session=db_session,
    )
    assert created is True
    await db_session.commit()
    return org.org_id, user.id, ticket_id, actor


# ---------------------------------------------------------------------------
# Service-function tests (db_session direct)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_attachment_with_frontmatter_populates_metadata(db_session) -> None:  # type: ignore[no-untyped-def]
    """Frontmatter fields land in the correct metadata columns."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    attachment = await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="requirements.md",
        body=_VALID_FRONTMATTER_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.commit()

    assert attachment.filename == "requirements.md"
    assert attachment.produced_by_skill == "pipeline-requirements"
    assert attachment.skill_version == "1.0.0"
    assert attachment.artifact_type == "requirements"
    assert attachment.repo_commit == "abc1234"
    assert attachment.produced_from is None
    assert attachment.note is None


@pytest.mark.asyncio
async def test_add_attachment_without_frontmatter_yields_all_none(db_session) -> None:  # type: ignore[no-untyped-def]
    """When the body carries no frontmatter, metadata columns are all None."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    attachment = await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="context.md",
        body=_PLAIN_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.commit()

    assert attachment.produced_by_skill is None
    assert attachment.skill_version is None
    assert attachment.artifact_type is None
    assert attachment.repo_commit is None


@pytest.mark.asyncio
async def test_add_attachment_malformed_frontmatter_degrades_gracefully(db_session) -> None:  # type: ignore[no-untyped-def]
    """Malformed frontmatter (wrong field type) degrades to context-only — no error."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    # Should not raise even though the frontmatter has a wrong type for `yaaos_artifact_version`.
    attachment = await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="malformed.md",
        body=_MALFORMED_FRONTMATTER_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.commit()

    assert attachment.produced_by_skill is None
    assert attachment.artifact_type is None


@pytest.mark.asyncio
async def test_add_attachment_over_cap_raises_too_large(db_session) -> None:  # type: ignore[no-untyped-def]
    """A body that exceeds 2 MiB raises AttachmentTooLargeError before any write."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    over_cap_body = "x" * (2 * 1024 * 1024 + 1)  # 1 byte over the 2 MiB cap

    with pytest.raises(AttachmentTooLargeError):
        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="big.md",
            body=over_cap_body,
            actor=actor,
            session=db_session,
        )


@pytest.mark.asyncio
async def test_add_attachment_rejects_unsafe_filenames(db_session) -> None:  # type: ignore[no-untyped-def]
    """A filename that is not a single safe path segment raises before any write.

    The filename is later joined as `.yaaos-inputs/<filename>` into a workspace
    write path by the run engine — a traversal segment would escape the inputs
    directory while staying inside the workspace root.
    """
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    for bad in (
        "../.git/hooks/pre-commit",
        "nested/file.md",
        "back\\slash.md",
        "..",
        ".",
        "",
        "dots..inside.md",
    ):
        with pytest.raises(InvalidAttachmentFilenameError):
            await add_attachment(
                ticket_id,
                org_id=org_id,
                filename=bad,
                body=_PLAIN_BODY,
                actor=actor,
                session=db_session,
            )


@pytest.mark.asyncio
async def test_add_attachment_accepts_plain_filename(db_session) -> None:  # type: ignore[no-untyped-def]
    """A plain single-segment filename is accepted."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    attachment = await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="requirements.v2.md",
        body=_PLAIN_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.commit()
    assert attachment.filename == "requirements.v2.md"


@pytest.mark.asyncio
async def test_add_attachment_unknown_ticket_raises_not_found(db_session) -> None:  # type: ignore[no-untyped-def]
    """A ticket_id that doesn't exist raises TicketNotFoundError."""
    from app.domain.orgs import insert_org as _insert_org  # noqa: PLC0415

    slug = f"att-nf-{uuid4().hex[:8]}"
    org = await _insert_org(db_session, slug=slug)
    await db_session.commit()
    user = await create_user(db_session, display_name="U")
    await db_session.commit()
    actor = Actor.user(user_id=user.id)

    with pytest.raises(TicketNotFoundError):
        await add_attachment(
            uuid4(),  # random ticket_id — does not exist
            org_id=org.org_id,
            filename="nf.md",
            body=_PLAIN_BODY,
            actor=actor,
            session=db_session,
        )


@pytest.mark.asyncio
async def test_list_attachments_returns_newest_first(db_session) -> None:  # type: ignore[no-untyped-def]
    """list_attachments orders by attached_at DESC."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="first.md",
        body=_PLAIN_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.flush()
    await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="second.md",
        body=_PLAIN_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.commit()

    metas = await list_attachments(ticket_id, org_id=org_id, session=db_session)

    assert len(metas) == 2
    # Newest first: second was added after first.
    filenames = [m.filename for m in metas]
    assert filenames.index("second.md") < filenames.index("first.md")

    # Bodies are NOT present in the metadata projection.
    assert not hasattr(metas[0], "body")


@pytest.mark.asyncio
async def test_get_attachment_cross_org_raises_not_found(db_session) -> None:  # type: ignore[no-untyped-def]
    """get_attachment with a different org_id raises AttachmentNotFoundError (no existence leak)."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    attachment = await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="secret.md",
        body=_PLAIN_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.commit()

    other_org_id = uuid4()
    with pytest.raises(AttachmentNotFoundError):
        await get_attachment(attachment.id, org_id=other_org_id, session=db_session)


@pytest.mark.asyncio
async def test_add_attachment_writes_audit_row(db_session) -> None:  # type: ignore[no-untyped-def]
    """After add_attachment + commit, an `attachment.added` audit row exists."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    attachment = await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="audited.md",
        body=_PLAIN_BODY,
        actor=actor,
        session=db_session,
    )
    await db_session.commit()

    entries = await list_for_entity("attachment", attachment.id, org_id=org_id)
    assert len(entries) == 1
    assert entries[0].kind == "attachment.added"
    assert entries[0].payload["filename"] == "audited.md"


@pytest.mark.asyncio
async def test_add_attachment_stashes_sse_event(db_session) -> None:  # type: ignore[no-untyped-def]
    """After add_attachment, an attachment_added event is stashed on the session for post-commit."""
    org_id, _, ticket_id, actor = await _make_org_user_ticket(db_session)

    await add_attachment(
        ticket_id,
        org_id=org_id,
        filename="sse-check.md",
        body=_PLAIN_BODY,
        actor=actor,
        session=db_session,
    )
    # Inspect the pending queue WITHOUT committing.  The key "yaaos_sse_general_pending"
    # is the value of `_GENERAL_AFTER_COMMIT_KEY` in `app.core.sse.service`; using the
    # string literal avoids a cross-module submodule import (Rule-6).
    pending = db_session.sync_session.info.get("yaaos_sse_general_pending", [])
    kinds = [k.value for (_, k, _) in pending]
    assert GeneralEventKind.ATTACHMENT_ADDED.value in kinds


# ---------------------------------------------------------------------------
# HTTP endpoint tests (httpx.ASGITransport)
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"attachments", "tickets"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_build_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):  # type: ignore[no-untyped-def]
    from app.domain.tickets import create_from_manual  # noqa: PLC0415

    user = await create_user(db_session, display_name="HTTP Tester")
    await db_session.flush()
    slug = f"att-http-{uuid4().hex[:8]}"
    org = await insert_org(db_session, slug=slug)
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="htstr")
    await db_session.flush()
    actor = Actor.user(user_id=user.id)
    ticket_id, _ = await create_from_manual(
        org_id=org.org_id,
        title="HTTP test ticket",
        repo_external_id="owner/repo",
        actor=actor,
        session=db_session,
    )
    sess = await mint_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "ticket_id": str(ticket_id),
        "session": sess,
    }


def _headers(seeded, *, mutate: bool = False) -> dict[str, str]:
    h = {"X-Yaaos-Org-Slug": seeded["org"].slug}
    if mutate:
        h["X-CSRF-Token"] = seeded["session"].csrf_token
    return h


def _cookies(seeded) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {
        "yaaos_session": seeded["session"].raw_token,
        "yaaos_csrf": seeded["session"].csrf_token,
    }


@pytest.mark.asyncio
async def test_post_attachment_returns_201_with_frontmatter(seeded) -> None:  # type: ignore[no-untyped-def]
    """POST /api/attachments returns 201 and `produced_by_skill` populated on valid frontmatter."""
    async with _client() as c:
        r = await c.post(
            "/api/attachments",
            json={
                "ticket_id": seeded["ticket_id"],
                "filename": "req.md",
                "body": _VALID_FRONTMATTER_BODY,
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["produced_by_skill"] == "pipeline-requirements"
    assert body["artifact_type"] == "requirements"
    assert "id" in body
    assert "attached_at" in body


@pytest.mark.asyncio
async def test_post_attachment_returns_201_null_for_plain(seeded) -> None:  # type: ignore[no-untyped-def]
    """POST /api/attachments returns 201 with `produced_by_skill: null` for plain body."""
    async with _client() as c:
        r = await c.post(
            "/api/attachments",
            json={
                "ticket_id": seeded["ticket_id"],
                "filename": "plain.md",
                "body": _PLAIN_BODY,
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 201, r.text
    assert r.json()["produced_by_skill"] is None


@pytest.mark.asyncio
async def test_post_attachment_too_large_returns_413(seeded) -> None:  # type: ignore[no-untyped-def]
    """POST /api/attachments with a body over 2 MiB returns 413 too_large."""
    over_cap_body = "x" * (2 * 1024 * 1024 + 1)
    async with _client() as c:
        r = await c.post(
            "/api/attachments",
            json={
                "ticket_id": seeded["ticket_id"],
                "filename": "big.md",
                "body": over_cap_body,
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["error"] == "too_large"


@pytest.mark.asyncio
async def test_post_attachment_traversal_filename_returns_400(seeded) -> None:  # type: ignore[no-untyped-def]
    """POST /api/attachments with a path-traversal filename returns 400 invalid_filename."""
    async with _client() as c:
        r = await c.post(
            "/api/attachments",
            json={
                "ticket_id": seeded["ticket_id"],
                "filename": "../.git/hooks/pre-commit",
                "body": _PLAIN_BODY,
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "invalid_filename"


@pytest.mark.asyncio
async def test_post_attachment_unknown_ticket_returns_404(seeded) -> None:  # type: ignore[no-untyped-def]
    """POST /api/attachments with an unknown ticket_id returns 404 ticket_not_found."""
    async with _client() as c:
        r = await c.post(
            "/api/attachments",
            json={
                "ticket_id": str(uuid4()),
                "filename": "nf.md",
                "body": _PLAIN_BODY,
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"] == "ticket_not_found"


@pytest.mark.asyncio
async def test_get_attachments_returns_list(seeded) -> None:  # type: ignore[no-untyped-def]
    """GET /api/attachments?ticket_id=… returns the attachments list."""
    async with _client() as c:
        # First create one.
        post_r = await c.post(
            "/api/attachments",
            json={
                "ticket_id": seeded["ticket_id"],
                "filename": "list-test.md",
                "body": _PLAIN_BODY,
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
        assert post_r.status_code == 201

        # Now list it.
        list_r = await c.get(
            "/api/attachments",
            params={"ticket_id": seeded["ticket_id"]},
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert list_r.status_code == 200, list_r.text
    data = list_r.json()
    assert "attachments" in data
    assert len(data["attachments"]) >= 1
    filenames = [a["filename"] for a in data["attachments"]]
    assert "list-test.md" in filenames
