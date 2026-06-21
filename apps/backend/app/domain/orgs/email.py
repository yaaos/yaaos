"""Minimal SMTP sender used by `domain/orgs` for invitation emails.

Dev points at Mailpit (`smtp://localhost:1025`); prod points at whatever SMTP
relay the operator configured. Synchronous `smtplib` wrapped in
`asyncio.to_thread` — invitation volume is low and `aiosmtplib` would only
add a dep for no real win.

In test env (`APP_MODE=test`), `send_plain` writes to an in-memory inbox
instead of hitting SMTP.  Two layers determine which inbox a given call sees:

1. **ContextVar override** (`_inbox_var`): `set_email_inbox_for_tests` installs
   a fresh `_Inbox` per unit test so tests are fully isolated from each other.
   The autouse `email_inbox_isolation` fixture in `app/testing/isolation` uses
   it.

2. **Module-global fallback** (`_global_inbox`): when no ContextVar override is
   set (e.g. in the e2e test stack where HTTP requests run in independent
   asyncio tasks), all callers fall back to the same module-global instance.
   Each HTTP request task inherits a copy of the root context where
   `_inbox_var` is unset, so without the global fallback different request
   tasks would each create their own isolated inbox and the e2e reader would
   never see the invite's email.

`read_sent_emails` returns the list from whichever inbox is active.
`clear_global_inbox` resets the module-global so the e2e reset endpoint can
guarantee a clean slate between test runs.
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


# Shared inbox for the e2e / test-stack path where no ContextVar override is
# installed.  Request tasks inherit a copy of the root context where
# `_inbox_var` is unset; the global fallback makes cross-request sharing work.
_global_inbox: _Inbox = _Inbox()

# Per-test override: `set_email_inbox_for_tests` binds a fresh instance here
# so unit tests are isolated from each other and from the global inbox.
_inbox_var: ContextVar[_Inbox | None] = ContextVar("_inbox_var", default=None)


def _get() -> _Inbox:
    """Return the active inbox: ContextVar override → global fallback."""
    val = _inbox_var.get()
    return val if val is not None else _global_inbox


def clear_global_inbox() -> None:
    """Clear the module-global inbox.

    Called by the e2e reset endpoint at the start of each test run to prevent
    emails from a previous (possibly failed) test from polluting the next run.
    """
    _global_inbox.messages.clear()


@contextmanager
def set_email_inbox_for_tests() -> Iterator[_Inbox]:
    """Context manager: bind a fresh email inbox for the duration.

    Each unit test gets a clean inbox isolated from the module-global and from
    every other test. Restores the prior binding on exit.
    """
    instance = _Inbox()
    token = _inbox_var.set(instance)
    try:
        yield instance
    finally:
        _inbox_var.reset(token)


def read_sent_emails() -> list[SentEmail]:
    """Return the live messages list from the current inbox.

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


__all__ = ["SentEmail", "clear_global_inbox", "read_sent_emails", "send_plain", "set_email_inbox_for_tests"]
