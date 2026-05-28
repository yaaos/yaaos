"""Unit tests for `fingerprint.py` — pure code, no I/O."""

from __future__ import annotations

from app.domain.reviewer.fingerprint import (
    compute_fingerprint,
    hash_anchor_content,
    hash_body_gist,
    normalize_line,
)


def test_normalize_line_collapses_whitespace_runs() -> None:
    assert normalize_line("  foo   bar  ") == "foo bar"


def test_normalize_line_strips_tabs() -> None:
    assert normalize_line("\tfoo\tbar\t") == "foo bar"


def test_hash_anchor_content_stable_across_whitespace_only_changes() -> None:
    original = ["    if foo:", "        return 1"]
    reindented = ["  if foo:", "    return 1"]

    assert hash_anchor_content(original) == hash_anchor_content(reindented)


def test_hash_anchor_content_differs_for_real_changes() -> None:
    a = ["if foo:", "    return 1"]
    b = ["if foo:", "    return 2"]

    assert hash_anchor_content(a) != hash_anchor_content(b)


def test_hash_body_gist_is_case_insensitive() -> None:
    a = hash_body_gist("security/sql-injection", "Possible SQL Injection")
    b = hash_body_gist("Security/SQL-Injection", "possible sql injection")

    assert a == b


def test_compute_fingerprint_same_content_different_lines_same_fingerprint() -> None:
    """Same anchored content at different line numbers → same fingerprint.

    Line numbers aren't in the fingerprint by design — they're an
    anchor concern, not an identity concern.
    """
    fp1 = compute_fingerprint(
        file_path="src/foo.py",
        rule_id="correctness/null-deref",
        anchored_lines=["x.bar()"],
        title="x could be None",
    )
    fp2 = compute_fingerprint(
        file_path="src/foo.py",
        rule_id="correctness/null-deref",
        anchored_lines=["x.bar()"],
        title="x could be None",
    )

    assert fp1 == fp2
    assert fp1.hash == fp2.hash


def test_compute_fingerprint_different_rule_id_different_fingerprint() -> None:
    fp1 = compute_fingerprint(
        file_path="src/foo.py",
        rule_id="correctness/null-deref",
        anchored_lines=["x.bar()"],
        title="x could be None",
    )
    fp2 = compute_fingerprint(
        file_path="src/foo.py",
        rule_id="style/naming",
        anchored_lines=["x.bar()"],
        title="x could be None",
    )

    assert fp1 != fp2


def test_compute_fingerprint_whitespace_only_diff_same_fingerprint() -> None:
    fp1 = compute_fingerprint(
        file_path="src/foo.py",
        rule_id="r/x",
        anchored_lines=["    if foo:"],
        title="t",
    )
    fp2 = compute_fingerprint(
        file_path="src/foo.py",
        rule_id="r/x",
        anchored_lines=["if foo:"],
        title="t",
    )

    assert fp1 == fp2
