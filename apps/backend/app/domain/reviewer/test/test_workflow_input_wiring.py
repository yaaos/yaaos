"""Verifies the M05 reviewer workflow definitions thread `$ticket.<field>`
inputs through to the Workspace command body. Pure plumbing test —
doesn't run a full workflow; checks that each workflow's step inputs
declare the expected references.

These references are part of the reviewer/intake contract: future intake
handlers MUST populate the named ticket-payload fields. The test
documents the contract and fails loudly if a workflow definition drifts.
"""

from __future__ import annotations

from app.domain.reviewer.workflows import (
    answer_question_v1,
    incremental_review_v1,
    pr_review_v1,
    stale_check_v1,
    verify_fix_v1,
)


def _step_inputs(workflow, step_id: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Return the inputs dict for the named step (str-only — workflows
    use string refs throughout)."""
    step = workflow.step_by_id(step_id)
    assert step is not None, f"workflow {workflow.name} has no step {step_id}"
    return dict(step.inputs)


def test_pr_review_v1_threads_head_and_base_sha_into_code_review() -> None:
    inputs = _step_inputs(pr_review_v1, "review")
    assert inputs["head_sha"] == "$ticket.head_sha"
    assert inputs["base_sha"] == "$ticket.base_sha"


def test_incremental_review_v1_threads_head_and_base_sha() -> None:
    inputs = _step_inputs(incremental_review_v1, "review")
    assert inputs["head_sha"] == "$ticket.head_sha"
    assert inputs["base_sha"] == "$ticket.base_sha"


def test_verify_fix_v1_threads_finding_id_and_head_sha() -> None:
    inputs = _step_inputs(verify_fix_v1, "verify")
    assert inputs["finding_id"] == "$ticket.finding_id"
    assert inputs["head_sha"] == "$ticket.head_sha"


def test_stale_check_v1_threads_finding_ids_and_head_sha() -> None:
    inputs = _step_inputs(stale_check_v1, "check")
    assert inputs["finding_ids"] == "$ticket.finding_ids"
    assert inputs["head_sha"] == "$ticket.head_sha"


def test_answer_question_v1_threads_finding_id_question_and_sha() -> None:
    answer = _step_inputs(answer_question_v1, "answer")
    assert answer["finding_id"] == "$ticket.finding_id"
    assert answer["question_body"] == "$ticket.question_body"
    assert answer["head_sha"] == "$ticket.head_sha"
    reply = _step_inputs(answer_question_v1, "reply")
    assert reply["finding_id"] == "$ticket.finding_id"
    assert reply["reply_body"] == "$answer.reply_body"
