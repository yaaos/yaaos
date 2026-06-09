"""Tests for plugin-internal prompt + state computation.

State computation (`_compute_state_v2`) is plugin-internal.
The remote-dispatch prompt is built by `build_review_invocation` — covered
by the integration tests in `test_pr_review_v1_e2e_service.py`.
"""

from __future__ import annotations

from app.core.coding_agent import ReportedFinding
from app.plugins.claude_code.service import _compute_state_v2


def _finding(severity: str) -> ReportedFinding:
    return ReportedFinding(
        file="src/foo.py",
        line=1,
        category="correctness",
        severity=severity,
        confidence="plausible",
        rationale="some rationale",
        rule_violated="rule/x",
        rule_source="yaaos",
        suggested_fix="fix it",
    )


def test_compute_state_v2_approved_when_no_findings() -> None:
    assert _compute_state_v2([]) == "APPROVED"


def test_compute_state_v2_changes_requested_on_blocker() -> None:
    findings = [_finding("nit"), _finding("blocker")]
    assert _compute_state_v2(findings) == "CHANGES_REQUESTED"


def test_compute_state_v2_comment_for_should_fix_and_nit() -> None:
    findings = [_finding("should_fix"), _finding("nit")]
    assert _compute_state_v2(findings) == "COMMENT"
