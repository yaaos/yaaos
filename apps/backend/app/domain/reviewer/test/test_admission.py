"""`admit_raw_findings` — wraps the aggregate's admission gate with the
repo lifecycle. Confirms the wrapper plumbing; the deep gate behaviour
(thresholds, off-diff, dedup, caps) is covered in `test_aggregate.py`.
"""

from __future__ import annotations

from uuid import uuid4

from app.domain.reviewer.admission import admit_raw_findings
from app.domain.reviewer.aggregate import RawFinding
from app.domain.reviewer.types import CodeAnchor, FindingFingerprint


def _short_scenario_raw() -> RawFinding:
    """A RawFinding whose `concrete_failure_scenario` is too short — the
    aggregate's schema gate (plan §10.1, 20-char minimum) drops it."""
    fp = FindingFingerprint(
        file_path="src/foo.py",
        rule_id="r1",
        anchor_content_hash="anc-r1-10",
        body_gist_hash="gist-r1-x",
    )
    return RawFinding(
        fingerprint=fp,
        rule_id="r1",
        title="t",
        body="b",
        rationale="r",
        concrete_failure_scenario="  ",  # too short → dropped
        confidence=90,
        severity="major",
        anchor=CodeAnchor(
            file_path="src/foo.py",
            line_start=10,
            line_end=10,
            surrounding_content_hash="surr-foo-10",
            commit_sha="abc123",
        ),
        source_agent="test",
    )


async def test_empty_raw_returns_empty_result(db_session) -> None:  # type: ignore[no-untyped-def]
    """Smoke test — wrapper round-trips through repo.load + save with no
    findings to process. Result has empty admitted/observations/drops."""
    result = await admit_raw_findings(
        pr_id=uuid4(),
        org_id=uuid4(),
        review_id=uuid4(),
        raw=[],
        session=db_session,
    )
    assert result.admitted == []
    assert result.observations == []
    assert result.drops == []


async def test_short_scenario_finding_dropped(db_session) -> None:  # type: ignore[no-untyped-def]
    """The aggregate's schema gate drops findings with too-short scenarios.
    Wrapper threads that drop through to `result.drops`."""
    result = await admit_raw_findings(
        pr_id=uuid4(),
        org_id=uuid4(),
        review_id=uuid4(),
        raw=[_short_scenario_raw()],
        session=db_session,
    )
    assert result.admitted == []
    assert len(result.drops) == 1
    assert result.drops[0].rule_id == "r1"
