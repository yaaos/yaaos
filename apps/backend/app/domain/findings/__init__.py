"""domain/findings — durable ticket-level findings.

A finding is materialized the moment a review iteration reports it and
carries a `open → resolved / dismissed` lifecycle with a full status-event
trail. Content lives here once, never copied into engine loop state.
"""

from app.domain.findings.service import (
    FindingNotFoundError,
    dismiss,
    evaluate_auto_approve,
    find_by_external_comment,
    get,
    list_for_stage_execution,
    list_open_for_ticket,
    mark_defended,
    record_findings,
    reflag,
    refresh_ticket_summary,
    reopen,
    resolve,
    set_external_anchor,
)
from app.domain.findings.types import (
    AutoApproveConditions,
    Finding,
    FindingSpec,
    FindingStatusEvent,
    InvalidFindingTransition,
)

__all__ = [
    "AutoApproveConditions",
    "Finding",
    "FindingNotFoundError",
    "FindingSpec",
    "FindingStatusEvent",
    "InvalidFindingTransition",
    "dismiss",
    "evaluate_auto_approve",
    "find_by_external_comment",
    "get",
    "list_for_stage_execution",
    "list_open_for_ticket",
    "mark_defended",
    "record_findings",
    "reflag",
    "refresh_ticket_summary",
    "reopen",
    "resolve",
    "set_external_anchor",
]
