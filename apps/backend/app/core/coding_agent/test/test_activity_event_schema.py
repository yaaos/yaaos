"""ActivityEvent schema — typed model guards for the activity log.

Tests that `ActivityEvent` enforces required fields, rejects unknown `kind`
values, and coerces ISO string timestamps to `datetime`. Does NOT require DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.coding_agent.types import ActivityEvent, ActivityLog


def _valid_event(**overrides) -> dict:
    base = {
        "seq": 0,
        "ts": datetime.now(UTC),
        "kind": "session_start",
        "message": "Session started · model opus",
    }
    base.update(overrides)
    return base


# ── Construction / required fields ────────────────────────────────────────────


def test_activity_event_constructs_with_required_fields() -> None:
    ev = ActivityEvent(**_valid_event())
    assert ev.seq == 0
    assert ev.kind == "session_start"
    assert ev.message == "Session started · model opus"
    assert ev.detail == {}


def test_activity_event_accepts_all_canonical_kinds() -> None:
    for kind in (
        "session_start",
        "subagent_dispatched",
        "tool_call_started",
        "assistant_message",
        "tool_call_finished",
        "result",
    ):
        ev = ActivityEvent(**_valid_event(kind=kind))
        assert ev.kind == kind


def test_activity_event_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ActivityEvent(**_valid_event(kind="made_up_kind"))


def test_activity_event_coerces_iso_string_ts() -> None:
    """Pydantic must coerce an ISO string to datetime — the stub bug this fixes."""
    iso = datetime.now(UTC).isoformat()
    ev = ActivityEvent(**_valid_event(ts=iso))
    assert isinstance(ev.ts, datetime)


def test_activity_event_rejects_malformed_ts() -> None:
    with pytest.raises(ValidationError):
        ActivityEvent(**_valid_event(ts="not-a-date"))


def test_activity_event_detail_accepts_dict() -> None:
    ev = ActivityEvent(**_valid_event(detail={"model": "opus", "session_id": "s"}))
    assert ev.detail["model"] == "opus"


# ── ActivityLog.events is list[ActivityEvent] ──────────────────────────────────


def test_activity_log_events_are_typed() -> None:
    now = datetime.now(UTC)
    log = ActivityLog(
        events=[
            ActivityEvent(seq=0, ts=now, kind="session_start", message="start"),
            ActivityEvent(seq=1, ts=now, kind="result", message="done"),
        ]
    )
    assert len(log.events) == 2
    assert all(isinstance(ev, ActivityEvent) for ev in log.events)


def test_activity_log_rejects_unknown_kind_in_events() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ActivityLog(events=[{"seq": 0, "ts": now, "kind": "bogus", "message": "x"}])


def test_activity_log_empty_by_default() -> None:
    log = ActivityLog()
    assert log.events == []
