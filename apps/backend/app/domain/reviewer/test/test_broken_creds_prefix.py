"""`_prefix_broken_creds_warning` prefixes the review summary with a yellow
GitHub callout listing affected MCP providers. No-op when nothing was observed.
"""

from __future__ import annotations

from uuid import uuid4

from app.domain.mcp_proxy import consume_broken_creds, record_broken_creds
from app.domain.reviewer.mcp_wiring import (
    prefix_broken_creds_warning as _prefix_broken_creds_warning,
)


def test_no_providers_returns_body_unchanged() -> None:
    assert _prefix_broken_creds_warning("hello", []) == "hello"
    assert _prefix_broken_creds_warning(None, []) is None


def test_with_providers_prefixes_warning() -> None:
    out = _prefix_broken_creds_warning("body text", ["linear", "notion"])
    assert out is not None
    assert out.startswith("> [!WARNING]")
    assert "**linear, notion**" in out
    assert "body text" in out


def test_warning_block_when_body_empty() -> None:
    out = _prefix_broken_creds_warning(None, ["linear"])
    assert out is not None
    assert "**linear**" in out


def test_record_and_consume_broken_creds_is_per_review() -> None:
    r1 = uuid4()
    r2 = uuid4()
    record_broken_creds(r1, "linear")
    record_broken_creds(r1, "notion")
    record_broken_creds(r2, "linear")
    assert consume_broken_creds(r1) == {"linear", "notion"}
    # Second consume on r1 is empty.
    assert consume_broken_creds(r1) == set()
    assert consume_broken_creds(r2) == {"linear"}
