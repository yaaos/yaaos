"""Cookie helpers — names, attributes, max-age.

`Secure` is env-gated off when `yaaos_env == "dev"` so `http://localhost` works
without TLS termination.
"""

from __future__ import annotations

from app.core.config import get_settings

SESSION_COOKIE_NAME = "yaaos_session"
CSRF_COOKIE_NAME = "yaaos_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def session_cookie_attrs(*, max_age_seconds: int) -> dict[str, object]:
    """Attributes for setting the session cookie via `Response.set_cookie`."""
    return {
        "key": SESSION_COOKIE_NAME,
        "max_age": max_age_seconds,
        "httponly": True,
        "samesite": "lax",
        "secure": get_settings().yaaos_env != "dev",
        "path": "/",
    }


def csrf_cookie_attrs(*, max_age_seconds: int) -> dict[str, object]:
    """The CSRF cookie is NOT HttpOnly — the SPA reads it to echo via header."""
    return {
        "key": CSRF_COOKIE_NAME,
        "max_age": max_age_seconds,
        "httponly": False,
        "samesite": "lax",
        "secure": get_settings().yaaos_env != "dev",
        "path": "/",
    }


def clear_cookie_attrs(name: str) -> dict[str, object]:
    return {
        "key": name,
        "max_age": 0,
        "path": "/",
    }
