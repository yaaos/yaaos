"""Minimal SMTP sender used by `domain/orgs` for invitation emails.

Dev points at Mailpit (`smtp://localhost:1025`); prod points at whatever SMTP
relay the operator configured. Synchronous `smtplib` wrapped in
`asyncio.to_thread` — invitation volume is low and `aiosmtplib` would only
add a dep for no real win.

The active `_Inbox` instance is held in a ContextVar. `_get()` lazily creates
one on first access in each context. `set_email_inbox_for_tests` is the
test seam; the `email_inbox_isolation` fixture in `app/testing/isolation`
uses it to bind a fresh instance per test. `send_plain` appends to the
ContextVar-bound inbox in test env instead of hitting SMTP. `read_sent_emails`
returns the list of captured messages.
"""

from __future__ import annotations

import asyncio
import smtplib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.message import EmailMessage

from app.core.config import get_settings


@dataclass(frozen=True, slots=True)
class SentEmail:
    to: str
    subject: str
    body: str


class _Inbox:
    """Captures emails in `test` env."""

    def __init__(self) -> None:
        self.messages: list[SentEmail] = []


_inbox_var: ContextVar[_Inbox | None] = ContextVar("_inbox_var", default=None)


def _get() -> _Inbox:
    val = _inbox_var.get()
    if val is None:
        val = _Inbox()
        _inbox_var.set(val)
    return val


@contextmanager
def set_email_inbox_for_tests() -> Iterator[_Inbox]:
    """Context manager: bind a fresh email inbox for the duration.

    Each test gets a clean inbox. Restores the prior binding on exit.
    """
    instance = _Inbox()
    token = _inbox_var.set(instance)
    try:
        yield instance
    finally:
        _inbox_var.reset(token)


def read_sent_emails() -> list[SentEmail]:
    """Return the live messages list from the current test inbox.

    Mutable — callers may call `.clear()` to reset between assertions
    within a single test body.
    """
    return _get().messages


def _send_blocking(msg: EmailMessage) -> None:
    s = get_settings()
    smtp = (
        smtplib.SMTP_SSL(s.smtp_host, s.smtp_port)
        if s.smtp_use_tls
        else smtplib.SMTP(s.smtp_host, s.smtp_port)
    )
    try:
        if s.smtp_username:
            smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
        smtp.send_message(msg)
    finally:
        smtp.quit()


async def send_plain(*, to: str, subject: str, body: str) -> None:
    """Send a plain-text email. In `test` env, append to the ContextVar-bound
    inbox and skip the SMTP round-trip; tests read the inbox via
    `read_sent_emails()`."""
    settings = get_settings()
    if settings.is_test:
        _get().messages.append(SentEmail(to=to, subject=subject, body=body))
        return
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    await asyncio.to_thread(_send_blocking, msg)


__all__ = ["SentEmail", "read_sent_emails", "send_plain", "set_email_inbox_for_tests"]
