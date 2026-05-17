"""Unit tests for `anchor.py` — line-drift resolution."""

from __future__ import annotations

import pytest

from app.domain.reviewer.anchor import hash_surrounding, make_anchor, resolve_anchor


def test_make_anchor_captures_surrounding_context() -> None:
    file_lines = ["line 1", "line 2", "line 3", "line 4", "line 5"]

    a = make_anchor(
        file_path="src/x.py",
        file_lines=file_lines,
        line_start=3,
        line_end=3,
        commit_sha="abc",
    )

    assert a.line_start == 3
    assert a.line_end == 3
    assert a.surrounding_content_hash  # populated


def test_resolve_anchor_same_position_when_unchanged() -> None:
    file_lines = ["a", "b", "c", "d", "e", "f", "g"]
    a = make_anchor(
        file_path="src/x.py",
        file_lines=file_lines,
        line_start=4,
        line_end=4,
        commit_sha="abc",
    )

    resolved = resolve_anchor(a, file_lines, "def")

    assert resolved is not None
    assert resolved.line_start == 4
    assert resolved.line_end == 4
    assert resolved.commit_sha == "def"


def test_resolve_anchor_finds_shifted_block() -> None:
    """When 2 lines are inserted at the top, the anchor shifts down by 2."""
    original = ["a", "b", "c", "TARGET", "e", "f", "g", "h"]
    shifted = ["new1", "new2", "a", "b", "c", "TARGET", "e", "f", "g", "h"]
    a = make_anchor(
        file_path="src/x.py",
        file_lines=original,
        line_start=4,
        line_end=4,
        commit_sha="abc",
    )

    resolved = resolve_anchor(a, shifted, "def")

    assert resolved is not None
    assert resolved.line_start == 6
    assert resolved.line_end == 6


def test_resolve_anchor_returns_none_when_gone() -> None:
    original = ["a", "b", "c", "TARGET", "e", "f", "g"]
    deleted = ["a", "b", "c", "e", "f", "g"]
    a = make_anchor(
        file_path="src/x.py",
        file_lines=original,
        line_start=4,
        line_end=4,
        commit_sha="abc",
    )

    assert resolve_anchor(a, deleted, "def") is None


def test_resolve_anchor_returns_none_when_ambiguous() -> None:
    """If the surrounding hash matches two windows, we can't pick — return None."""
    original = ["x", "TARGET", "y"]
    duplicated = ["x", "TARGET", "y", "x", "TARGET", "y"]
    a = make_anchor(
        file_path="src/x.py",
        file_lines=original,
        line_start=2,
        line_end=2,
        commit_sha="abc",
    )

    assert resolve_anchor(a, duplicated, "def") is None


def test_hash_surrounding_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="out of range"):
        hash_surrounding(["a", "b"], 1, 5)
