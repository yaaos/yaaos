"""`parse_review_output` lives in `domain/reviewer` and is the canonical parser.

Covers:
1. Valid stdout → list of `ReportedFinding` with correct field values.
2. Invalid stdout (no terminal `result` event) → `ValueError`.
3. Stdout with a terminal event whose `result` is not valid JSON → `ValueError`.
4. Stdout with a terminal event whose `result` does not match `FindingDraftList` → `ValueError`.
"""

from __future__ import annotations

import json

import pytest

from app.domain.reviewer import ReportedFinding, parse_review_output


def _stream_json_with_result(result_payload: object) -> str:
    """Build a minimal stream-json string with a single `type=result` event."""
    event = {"type": "result", "result": json.dumps(result_payload)}
    return json.dumps(event) + "\n"


def _valid_finding_payload() -> dict:
    """One conforming finding dict."""
    return {
        "findings": [
            {
                "file": "src/auth.py",
                "line": 42,
                "category": "security",
                "severity": "blocker",
                "confidence": "verified",
                "rationale": "SQL injection risk",
                "rule_violated": "OWASP-A1",
                "rule_source": "owasp",
                "suggested_fix": "Use parameterized queries",
            }
        ]
    }


def test_valid_stdout_returns_reported_findings() -> None:
    """Valid stream-json with a conforming `FindingDraftList` result → list[ReportedFinding]."""
    stdout = _stream_json_with_result(_valid_finding_payload())
    findings = parse_review_output(stdout)

    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, ReportedFinding)
    assert f.file == "src/auth.py"
    assert f.line == 42
    assert f.category == "security"
    assert f.severity == "blocker"
    assert f.confidence == "verified"
    assert f.rationale == "SQL injection risk"
    assert f.rule_violated == "OWASP-A1"
    assert f.rule_source == "owasp"
    assert f.suggested_fix == "Use parameterized queries"


def test_null_anchor_finding_is_accepted() -> None:
    """A finding without `file`/`line` (PR-wide) is accepted."""
    payload = {
        "findings": [
            {
                "file": None,
                "line": None,
                "category": "architecture",
                "severity": "nit",
                "confidence": "speculative",
                "rationale": "General concern",
                "rule_violated": "arch-001",
                "rule_source": "internal",
                "suggested_fix": "Consider refactoring",
            }
        ]
    }
    stdout = _stream_json_with_result(payload)
    findings = parse_review_output(stdout)

    assert len(findings) == 1
    assert findings[0].file is None
    assert findings[0].line is None


def test_no_result_event_raises_value_error() -> None:
    """Stdout with no `type=result` event raises `ValueError`."""
    stdout = json.dumps({"type": "text", "text": "hello"}) + "\n"
    with pytest.raises(ValueError, match="no 'type=result' event"):
        parse_review_output(stdout)


def test_empty_stdout_raises_value_error() -> None:
    """Empty stdout raises `ValueError` (no result event)."""
    with pytest.raises(ValueError):
        parse_review_output("")


def test_result_field_not_valid_json_raises_value_error() -> None:
    """A `result` field that is not valid JSON raises `ValueError`."""
    event = {"type": "result", "result": "this is not json {{{"}
    stdout = json.dumps(event) + "\n"
    with pytest.raises(ValueError, match="FindingDraftList"):
        parse_review_output(stdout)


def test_result_field_wrong_schema_raises_value_error() -> None:
    """A `result` JSON that doesn't match `FindingDraftList` raises `ValueError`."""
    # Missing required fields on each finding
    payload = {"findings": [{"file": "src/foo.py"}]}
    stdout = _stream_json_with_result(payload)
    with pytest.raises(ValueError, match="FindingDraftList"):
        parse_review_output(stdout)


def test_uses_last_result_event() -> None:
    """When multiple events are present, the last `type=result` event is used."""
    first_event = {"type": "result", "result": json.dumps({"findings": []})}
    second_event = _valid_finding_payload()
    # second result event carries the real finding
    second_result_event = {"type": "result", "result": json.dumps(second_event)}
    stdout = json.dumps(first_event) + "\n" + json.dumps(second_result_event) + "\n"
    findings = parse_review_output(stdout)
    assert len(findings) == 1
    assert findings[0].category == "security"
