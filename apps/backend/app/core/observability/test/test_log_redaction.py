"""Log redaction — secrets never reach the renderer or the OTLP export pipe.

Two gates:
- `_redact_secrets` (structlog processor, runs in `foreign_pre_chain`) masks
  secret-keyed values on the stdout pipe for both app and foreign records.
- `_SecretScrubFilter` (stdlib root-logger filter) masks secret-keyed values on
  foreign records headed for the OTel `LoggingHandler`, which bypasses the
  structlog `ProcessorFormatter`.

`_AccessLogDebugFilter` demotes `uvicorn.access` records to DEBUG so production
(LOG_LEVEL=INFO) drops them.
"""

from __future__ import annotations

import logging

from app.core.observability.service import (
    _AccessLogDebugFilter,
    _redact_secrets,
    _SecretScrubFilter,
)

# ── _redact_secrets (structlog processor) ────────────────────────────────────


def test_redacts_authorization_header_value() -> None:
    out = _redact_secrets(None, "info", {"authorization": "Bearer abc123"})
    assert out["authorization"] == "***"


def test_redacts_nested_bearer_field() -> None:
    out = _redact_secrets(None, "info", {"headers": {"Authorization": "Bearer xyz"}})
    assert out["headers"]["Authorization"] == "***"


def test_redacts_token_signed_request_password() -> None:
    payload = {
        "token": "secretvalue",
        "signed_request": '{"headers":{"Authorization":"AWS4..."}}',
        "user": {"password": "hunter2", "api_key": "k1", "name": "alice"},
    }
    out = _redact_secrets(None, "info", payload)
    assert out["token"] == "***"
    assert out["signed_request"] == "***"
    assert out["user"]["password"] == "***"
    assert out["user"]["api_key"] == "***"
    assert out["user"]["name"] == "alice"


def test_leaves_unrelated_keys_alone() -> None:
    out = _redact_secrets(None, "info", {"agent_id": "uuid", "event": "ok", "count": 5})
    assert out == {"agent_id": "uuid", "event": "ok", "count": 5}


def test_list_of_dicts_scrubbed_per_element() -> None:
    out = _redact_secrets(None, "info", {"items": [{"authorization": "x"}, {"k": "v"}]})
    assert out["items"][0]["authorization"] == "***"
    assert out["items"][1]["k"] == "v"


# ── _SecretScrubFilter (OTLP-path stdlib filter) ─────────────────────────────


def _record(msg: object, args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,  # type: ignore[arg-type]
        exc_info=None,
    )


def test_scrub_filter_masks_dict_arg() -> None:
    """A header dict interpolated as a positional arg is masked before export."""
    record = _record("request failed: %s", ({"Authorization": "Bearer leak"},))
    assert _SecretScrubFilter().filter(record) is True
    assert record.getMessage() == "request failed: {'Authorization': '***'}"


def test_scrub_filter_masks_dict_msg() -> None:
    record = _record({"token": "leak", "path": "/ok"}, None)
    assert _SecretScrubFilter().filter(record) is True
    assert record.msg == {"token": "***", "path": "/ok"}


def test_scrub_filter_leaves_plain_message_intact() -> None:
    """Free-text messages with no structured payload pass through unchanged."""
    record = _record("GET /tickets 200", None)
    assert _SecretScrubFilter().filter(record) is True
    assert record.getMessage() == "GET /tickets 200"


# ── _AccessLogDebugFilter ────────────────────────────────────────────────────


def test_access_log_filter_demotes_to_debug() -> None:
    record = _record("GET / 200", None)
    assert record.levelno == logging.INFO
    assert _AccessLogDebugFilter().filter(record) is True
    assert record.levelno == logging.DEBUG
    assert record.levelname == "DEBUG"
