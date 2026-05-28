"""verify_fix must give the agent the ORIGINAL anchored code
(captured when the finding was raised) so it can compare against the
current code, rather than a placeholder the LLM would have to guess
around.
"""

from __future__ import annotations

from app.domain.reviewer.anchor import make_anchor


def test_make_anchor_captures_original_lines() -> None:
    file_lines = ["before 1", "before 2", "before 3", "ANCHORED A", "ANCHORED B", "after 1"]
    a = make_anchor(
        file_path="src/foo.py",
        file_lines=file_lines,
        line_start=4,
        line_end=5,
        commit_sha="abc",
    )
    assert a.original_lines == ("ANCHORED A", "ANCHORED B")


def test_original_lines_survives_resolve_anchor() -> None:
    """When the anchor relocates after a line shift, the ORIGINAL code
    snapshot must carry through. verify_fix asks the agent to compare
    `original_lines` against the lines at the new (resolved) position.
    """
    from app.domain.reviewer.anchor import resolve_anchor  # noqa: PLC0415

    original_file = [
        "unrelated 1",
        "unrelated 2",
        "above 1",
        "above 2",
        "above 3",
        "ANCHOR HERE",
        "below 1",
        "below 2",
        "below 3",
        "unrelated 3",
    ]
    anchor = make_anchor(
        file_path="src/foo.py",
        file_lines=original_file,
        line_start=6,
        line_end=6,
        commit_sha="old",
    )
    assert anchor.original_lines == ("ANCHOR HERE",)

    # Same block but shifted 4 lines down.
    new_file = ["new top"] * 4 + original_file
    moved = resolve_anchor(anchor, new_file, "new")
    assert moved is not None
    assert moved.line_start == 10  # 6 + 4
    assert moved.original_lines == ("ANCHOR HERE",), (
        "resolve_anchor must carry forward the ORIGINAL lines — "
        "they describe the finding's source code, not the current code."
    )
