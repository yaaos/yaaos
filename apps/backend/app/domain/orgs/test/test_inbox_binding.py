"""Service-level coverage of the ContextVar-based email inbox isolation."""

from __future__ import annotations

import pytest

from app.domain.orgs.email import send_plain, set_email_inbox_for_tests
from app.testing.seed import read_email_inbox

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
