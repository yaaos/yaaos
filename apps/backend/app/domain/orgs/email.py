"""Minimal SMTP sender used by `domain/orgs` for invitation emails.

Dev points at Mailpit (`smtp://localhost:1025`); prod points at whatever SMTP
relay the operator configured. Synchronous `smtplib` wrapped in
`asyncio.to_thread` — invitation volume is low and `aiosmtplib` would only
add a dep for no real win.

The active `_Inbox` instance is ContextVar-bound. `bind_email_inbox` is the
production DI seam — the composition root calls it at startup; the
`email_inbox_isolation` fixture in `app/testing/isolation` binds a fresh
instance per test and exposes a `read_email_inbox` accessor. `send_plain`
writes to the ContextVar-bound inbox in test env instead of hitting SMTP.
`get_email_inbox()` raises `RuntimeError` if called before any bind in test env.
"""

from __future__ import annotations

import asyncio
import smtplib
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.message import EmailMessage

from app.core.config import get_settings


@dataclass(frozen=True, slots=True)
class SentEmail:
    to: str
    subject: str
    body: str


@dataclass
class _Inbox:
    """Captures emails in `test` env."""

    messages: list[SentEmail] = field(default_factory=list)


_inbox_var: ContextVar[_Inbox | None] = ContextVar("_inbox_var", default=None)


def bind_email_inbox(instance: _Inbox) -> None:
    """Bind `instance` as the active email inbox for the current Context.

    Called once at process startup (composition root) and once per test
    (isolation fixture). Subsequent calls in the same Context replace the
    prior binding.
    """
    _inbox_var.set(instance)


def get_email_inbox() -> _Inbox:
    """Return the active email inbox. Raises `RuntimeError` if
    `bind_email_inbox` has not been called — fail-fast so forgotten startup
    binds surface immediately rather than silently dropping emails."""
    instance = _inbox_var.get()
    if instance is None:
        raise RuntimeError(
            "email inbox not bound: call bind_email_inbox(_Inbox()) at process "
            "startup or use the email_inbox_isolation fixture in tests."
        )
    return instance


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
    inbox and skip the SMTP round-trip; tests read the inbox via the
    `email_inbox_isolation` fixture's accessor."""
    settings = get_settings()
    if settings.yaaos_env == "test":
        get_email_inbox().messages.append(SentEmail(to=to, subject=subject, body=body))
        return
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    await asyncio.to_thread(_send_blocking, msg)


__all__ = ["SentEmail", "_Inbox", "bind_email_inbox", "get_email_inbox", "send_plain"]
