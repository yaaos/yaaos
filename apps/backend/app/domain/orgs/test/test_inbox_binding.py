"""Service-level coverage of the ContextVar-based email inbox binding."""

from __future__ import annotations

import pytest

from app.domain.orgs.email import _Inbox, bind_email_inbox, get_email_inbox, send_plain
from app.testing.seed import read_email_inbox

# ── Binding / isolation ────────────────────────────────────────────────


def test_fresh_bind_gives_empty_inbox() -> None:
    """The autouse fixture binds a fresh inbox per test — starts empty."""
    assert read_email_inbox() == []


@pytest.mark.asyncio
async def test_send_plain_writes_to_bound_inbox() -> None:
    """send_plain in test env appends to the ContextVar-bound inbox."""
    await send_plain(to="a@example.com", subject="Hello", body="World")
    inbox = read_email_inbox()
    assert len(inbox) == 1
    assert inbox[0].to == "a@example.com"
    assert inbox[0].subject == "Hello"
    assert inbox[0].body == "World"


@pytest.mark.asyncio
async def test_fresh_bind_hides_prior_messages() -> None:
    """Binding a new _Inbox after a send hides the prior messages —
    isolation works across explicit re-binds within a test."""
    await send_plain(to="before@example.com", subject="s", body="b")
    assert len(read_email_inbox()) == 1

    # Rebind — prior messages gone.
    bind_email_inbox(_Inbox())
    assert read_email_inbox() == []


def test_get_email_inbox_raises_before_bind() -> None:
    """Deliberately unbind and verify the fail-fast RuntimeError fires."""
    from app.domain.orgs.email import _inbox_var  # noqa: PLC0415

    token = _inbox_var.set(None)
    try:
        with pytest.raises(RuntimeError, match="email inbox not bound"):
            get_email_inbox()
    finally:
        _inbox_var.reset(token)
        bind_email_inbox(_Inbox())


@pytest.mark.asyncio
async def test_multiple_sends_accumulate() -> None:
    """Multiple send_plain calls in one test body accumulate in the inbox."""
    await send_plain(to="x@example.com", subject="s1", body="b1")
    await send_plain(to="y@example.com", subject="s2", body="b2")
    inbox = read_email_inbox()
    assert len(inbox) == 2
    assert {m.to for m in inbox} == {"x@example.com", "y@example.com"}
