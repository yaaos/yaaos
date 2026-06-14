"""Shared types for the task pipeline.

`TaskMetadata` is the typed envelope carried on the taskiq label
`metadata` from `enqueue` (producer) through the outbox + drain to
`OrgContextMiddleware` (consumer). Replaces the prior dict-via-repr
encoding: producer dumps via `model_dump_json()`, consumer parses via
`model_validate_json()` — JSON in/out, no `ast.literal_eval` round-trip.

Tests that call `pre_execute` directly with a raw dict still work —
`model_validate` accepts both dict and JSON-string inputs.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class TaskMetadata(BaseModel):
    """Per-task envelope.

    `org_id` ties the task to an org so the worker can enter `org_context`
    before the body runs. Optional — tasks enqueued outside any org context
    (e.g. boot-time system tasks) may omit it.

    `traceparent` is the W3C traceparent of the enqueuing span. `enqueue`
    auto-fills this from `current_traceparent()` so `TaskSpanMiddleware`
    can open the `task:<name>` span as a child of the producer's span —
    landing all task spans in the producer's trace rather than orphan traces.
    Optional — absent when no OTel SDK is active or no span is in scope.
    """

    model_config = {"frozen": True}

    org_id: UUID | None = None
    # W3C traceparent format is exactly 55 chars (`00-<32hex>-<16hex>-<02hex>`);
    # cap the length so a malformed outbox payload can't waste memory at parse.
    # `restore_traceparent_context` discards malformed values via the OTel propagator.
    traceparent: str | None = Field(default=None, max_length=55)
