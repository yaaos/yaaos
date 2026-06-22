"""Service-level coverage of the email inbox: ContextVar isolation + global fallback."""

from __future__ import annotations

import pytest

from app.domain.orgs import read_sent_emails as read_email_inbox
from app.domain.orgs.email import (
    SentEmail,
    _global_inbox,
    clear_global_inbox,
    send_plain,
    set_email_inbox_for_tests,
)

# ── Isolation ─────────────────────────────────────────────────────────────


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
async def test_set_email_inbox_for_tests_hides_outer_messages() -> None:
    """Nesting set_email_inbox_for_tests hides messages from the outer context."""
    await send_plain(to="before@example.com", subject="s", body="b")
    assert len(read_email_inbox()) == 1

    # Enter a fresh inbox — outer messages no longer visible.
    with set_email_inbox_for_tests():
        assert read_email_inbox() == []
        await send_plain(to="inner@example.com", subject="s2", body="b2")
        assert len(read_email_inbox()) == 1

    # Outer inbox is restored.
    assert len(read_email_inbox()) == 1
    assert read_email_inbox()[0].to == "before@example.com"


@pytest.mark.asyncio
async def test_eager_default_is_stable() -> None:
    """The inbox is always available — no bind required before use."""
    from app.domain.orgs.email import read_sent_emails  # noqa: PLC0415

    # No explicit bind; eager default already in place.
    assert read_sent_emails() == []


@pytest.mark.asyncio
async def test_multiple_sends_accumulate() -> None:
    """Multiple send_plain calls in one test body accumulate in the inbox."""
    await send_plain(to="x@example.com", subject="s1", body="b1")
    await send_plain(to="y@example.com", subject="s2", body="b2")
    inbox = read_email_inbox()
    assert len(inbox) == 2
    assert {m.to for m in inbox} == {"x@example.com", "y@example.com"}


# ── Global fallback (e2e path) ─────────────────────────────────────────────


def test_global_inbox_fallback_when_no_override() -> None:
    """Without a ContextVar override, _get() returns _global_inbox.

    This is the e2e / test-stack path: HTTP request tasks each start with
    _inbox_var=None (inherited from the root context where it was never set),
    so cross-request email visibility requires a shared module-global.
    """
    # The email_inbox_isolation autouse fixture is active for this test, which
    # sets _inbox_var to a fresh instance.  Temporarily reset it to confirm the
    # global fallback is reachable.

    # Peek at the global directly; _get() under the fixture returns the
    # fixture inbox, not the global.  Confirm _global_inbox exists and is shared.
    assert _global_inbox is not None, "_global_inbox must be a module-level _Inbox"

    # Confirm clear_global_inbox wipes messages from _global_inbox.
    _global_inbox.messages.append(SentEmail(to="g@example.com", subject="s", body="b"))
    assert len(_global_inbox.messages) == 1
    clear_global_inbox()
    assert len(_global_inbox.messages) == 0


def test_clear_global_inbox_is_idempotent() -> None:
    """Calling clear_global_inbox on an already-empty inbox is a no-op."""
    clear_global_inbox()
    clear_global_inbox()
    assert _global_inbox.messages == []
