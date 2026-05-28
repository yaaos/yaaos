"""`CodeAnchor` re-resolution under line drift.

When a file changes between reviews, line numbers drift. The anchor's
`surrounding_content_hash` covers 3 lines of context above + the anchored
range + 3 lines below, whitespace-normalized. We re-find the anchor in the
new file by scanning every candidate window of the same length and comparing
the surrounding hash.

`resolve_anchor` returns:
- `(new_line_start, new_line_end)` if a unique match exists (gone → moved).
- `None` if the surrounding hash isn't found (gone → `stale` or `resolved_unverified`).
"""

from __future__ import annotations

import hashlib

from app.domain.reviewer.fingerprint import normalize_line
from app.domain.reviewer.types import CodeAnchor

_CONTEXT_LINES = 3


def hash_surrounding(
    file_lines: list[str], line_start: int, line_end: int, *, context: int = _CONTEXT_LINES
) -> str:
    """sha256 of `context` lines above + anchored range + `context` lines below.

    `line_start` / `line_end` are 1-based and inclusive. Out-of-bounds context
    clips to file boundaries.
    """
    if line_start < 1 or line_end < line_start or line_end > len(file_lines):
        raise ValueError(f"anchor [{line_start},{line_end}] out of range for {len(file_lines)}-line file")

    above_start = max(0, line_start - 1 - context)
    below_end = min(len(file_lines), line_end + context)
    window = file_lines[above_start:below_end]
    normalized = "\n".join(normalize_line(line) for line in window)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def make_anchor(
    *,
    file_path: str,
    file_lines: list[str],
    line_start: int,
    line_end: int,
    commit_sha: str,
) -> CodeAnchor:
    """Build a `CodeAnchor` for a fresh observation.

    Captures `original_lines` (the exact anchored range as a tuple) so the
    verify_fix subflow can compare against the developer's edited code
    without re-reading historical commits.
    """
    return CodeAnchor(
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        surrounding_content_hash=hash_surrounding(file_lines, line_start, line_end),
        commit_sha=commit_sha,
        original_lines=tuple(file_lines[line_start - 1 : line_end]),
    )


def resolve_anchor(
    old_anchor: CodeAnchor,
    new_file_lines: list[str],
    new_commit_sha: str,
) -> CodeAnchor | None:
    """Re-find `old_anchor` in `new_file_lines`. Returns a new anchor or None.

    Strategy: try the same line range first (cheap fast path); if its hash
    matches, the file is unchanged at this anchor. Otherwise scan every
    candidate window of the same length and return the first match with the
    same `surrounding_content_hash`. If multiple match, the anchor is
    ambiguous and we conservatively return None — caller treats as gone.
    """
    span = old_anchor.line_end - old_anchor.line_start + 1

    if old_anchor.line_end <= len(new_file_lines):
        try:
            same_position = hash_surrounding(new_file_lines, old_anchor.line_start, old_anchor.line_end)
        except ValueError:
            same_position = ""
        if same_position == old_anchor.surrounding_content_hash:
            return CodeAnchor(
                file_path=old_anchor.file_path,
                line_start=old_anchor.line_start,
                line_end=old_anchor.line_end,
                surrounding_content_hash=old_anchor.surrounding_content_hash,
                commit_sha=new_commit_sha,
                original_lines=old_anchor.original_lines,
            )

    matches: list[int] = []
    for candidate_start in range(1, len(new_file_lines) - span + 2):
        candidate_end = candidate_start + span - 1
        try:
            h = hash_surrounding(new_file_lines, candidate_start, candidate_end)
        except ValueError:
            continue
        if h == old_anchor.surrounding_content_hash:
            matches.append(candidate_start)
            if len(matches) > 1:
                return None  # ambiguous; caller treats as gone

    if not matches:
        return None
    start = matches[0]
    return CodeAnchor(
        file_path=old_anchor.file_path,
        line_start=start,
        line_end=start + span - 1,
        surrounding_content_hash=old_anchor.surrounding_content_hash,
        commit_sha=new_commit_sha,
        original_lines=old_anchor.original_lines,
    )
