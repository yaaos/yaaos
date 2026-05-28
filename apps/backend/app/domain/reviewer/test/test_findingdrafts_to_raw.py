"""anchor_content_hash hashes actual file content at the anchored line
range, NOT the finding's body text. Different body phrasings for the
same anchored code must produce IDENTICAL fingerprints so the aggregate
deduplicates re-observations across reviews.

Guards against using the body/title as the "anchored lines", which
churns the fingerprint every time the model rephrases the finding body.
"""

from __future__ import annotations

from app.domain.coding_agent import FindingAnchor, FindingDraft
from app.domain.reviewer.admission import findingdrafts_to_raw as _findingdrafts_to_raw


def _draft(body: str) -> FindingDraft:
    return FindingDraft(
        severity="major",
        rule_id="r/x",
        title="t",
        body=body,
        concrete_failure_scenario="caller invokes f() without arg; raises TypeError.",
        confidence=90,
        rationale="r",
        anchor=FindingAnchor(file_path="src/foo.py", line_start=10, line_end=10),
        duplicate_of_rule_ids=[],
    )


def test_fingerprint_is_stable_across_body_rephrasings() -> None:
    """Same file + same anchor + same rule_id → identical fingerprint
    regardless of how the model phrased the body.
    """
    file_lines = [f"line {i}" for i in range(20)]
    file_lines[9] = "x = config.get('key')"  # the anchored line at 10 (1-based)

    draft_a = _draft("this can raise KeyError when key is missing")
    draft_b = _draft("KeyError is possible here if `key` isn't in config")

    raw_a = _findingdrafts_to_raw(
        [draft_a], commit_sha="abc", read_file=lambda p: file_lines if p == "src/foo.py" else None
    )
    raw_b = _findingdrafts_to_raw(
        [draft_b], commit_sha="abc", read_file=lambda p: file_lines if p == "src/foo.py" else None
    )

    assert raw_a[0].fingerprint.hash == raw_b[0].fingerprint.hash, (
        "Different body phrasings must not change the fingerprint — "
        "anchor_content_hash must be derived from file content, not body."
    )


def test_anchor_surrounding_hash_uses_file_content_not_body() -> None:
    """Two findings at the same anchor but different bodies must share the
    anchor's `surrounding_content_hash` — it hashes ±3 lines from the file.
    """
    file_lines = [f"line {i}" for i in range(20)]
    file_lines[9] = "x = config.get('key')"

    draft_a = _draft("body version A")
    draft_b = _draft("body version B")

    raw_a = _findingdrafts_to_raw([draft_a], commit_sha="abc", read_file=lambda p: file_lines)
    raw_b = _findingdrafts_to_raw([draft_b], commit_sha="abc", read_file=lambda p: file_lines)

    assert raw_a[0].anchor.surrounding_content_hash == raw_b[0].anchor.surrounding_content_hash


def test_empty_file_returns_no_raw_findings() -> None:
    """Regression: `ws.read_text("")` on an empty file returns `""` → `splitlines()`
    yields `[]`. The drop guard must treat empty content the same as missing —
    otherwise `make_anchor` raises `ValueError` against a 0-line file and the
    whole admission step crashes mid-review.
    """
    draft = _draft("body")
    raw = _findingdrafts_to_raw([draft], commit_sha="abc", read_file=lambda p: [])
    assert raw == []


def test_file_missing_returns_no_raw_findings() -> None:
    """If we can't read the file (deleted, binary, traversal), drop the draft —
    we cannot make a stable fingerprint without real content.
    """
    draft = _draft("body")
    raw = _findingdrafts_to_raw([draft], commit_sha="abc", read_file=lambda p: None)
    assert raw == []
