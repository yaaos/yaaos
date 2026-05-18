"""`_DomainEventEnvelope.wrap` must produce JSON-safe payloads.

Plan §5.2: domain events flow through `core/events`. The Pydantic-based bus
serializes events; UUIDs and StrEnums must survive `model_dump(mode="json")`
without raising. The legacy `asdict(event)` approach left UUIDs/enums as
raw Python types — silent breakage at publish time.
"""

from __future__ import annotations

import json
import uuid

from app.domain.reviewer.events import (
    FindingAcknowledged,
    FindingRaised,
    FindingStateChanged,
)
from app.domain.reviewer.service import _DomainEventEnvelope
from app.domain.reviewer.types import FindingState


def test_envelope_uuid_payload_serializes_as_string() -> None:
    finding_id = uuid.uuid4()
    pr_id = uuid.uuid4()
    env = _DomainEventEnvelope.wrap(FindingRaised(finding_id=finding_id, pr_id=pr_id))

    # Round-trip through JSON — the bus does this for SSE.
    blob = env.model_dump_json()
    parsed = json.loads(blob)

    assert parsed["payload"]["finding_id"] == str(finding_id)
    assert parsed["payload"]["pr_id"] == str(pr_id)


def test_envelope_state_enum_payload_serializes_as_string() -> None:
    env = _DomainEventEnvelope.wrap(
        FindingStateChanged(
            finding_id=uuid.uuid4(),
            from_state=FindingState.OPEN,
            to_state=FindingState.ACKNOWLEDGED,
        )
    )
    parsed = json.loads(env.model_dump_json())
    assert parsed["payload"]["from_state"] == "open"
    assert parsed["payload"]["to_state"] == "acknowledged"


def test_envelope_ackkind_enum_payload_serializes_as_string() -> None:
    env = _DomainEventEnvelope.wrap(
        FindingAcknowledged(
            finding_id=uuid.uuid4(),
            ack_id=uuid.uuid4(),
            kind="wontfix",
        )
    )
    parsed = json.loads(env.model_dump_json())
    assert parsed["payload"]["kind"] == "wontfix"
