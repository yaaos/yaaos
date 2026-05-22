"""`core.events.Event` subclasses for the legacy review-job runner.

Distinct from `domain/reviewer/events.py`, which holds dataclass-shaped
aggregate domain events (`ReviewRequested`, `FindingRaised`, etc.).

These Pydantic Event types carry per-review-job lifecycle signals across
the SSE bus + audit pipeline. They originated in `queue.py` and are
extracted here so subscribers (`replies.py`, `incremental.py`) can
import them without depending on the legacy file.

When the queue.py dismantle completes, these types may move to a
workflow-engine-flavored module or be removed if the engine path emits
its own equivalents.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from app.core.events import Event


class ReviewJobStatusChanged(Event):
    kind: Literal["review_job_status_changed"] = "review_job_status_changed"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    review_job_id: UUID
    status: str


class ReviewJobStepProgress(Event):
    """In-place row update — not an audit entry. Drives the running-state UI."""

    kind: Literal["review_job_step_progress"] = "review_job_step_progress"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    review_job_id: UUID
    current_step: str


class ReviewJobActivity(Event):
    """One captured stream event from the coding-agent CLI.

    High-frequency (~50-100 per review). Not persisted as an audit entry —
    the per-row `activity_log` JSONB column carries the durable copy. SSE
    consumers push events into a local store keyed by review_job_id.
    """

    kind: Literal["review_job_activity"] = "review_job_activity"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    review_job_id: UUID
    event: dict[str, Any]


__all__ = [
    "ReviewJobActivity",
    "ReviewJobStatusChanged",
    "ReviewJobStepProgress",
]
