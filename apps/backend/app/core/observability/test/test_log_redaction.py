"""Logging redaction — secrets never reach the renderer raw."""

from __future__ import annotations

from app.core.observability.service import _redact_secrets


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
