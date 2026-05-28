"""Pure helpers that inspect a `Diff` and the surrounding PR shape.

Shared by the workflow-engine path and the runner. No DB, no async, no
plugin lookups — input → output only.

- `detect_language(diff)` — pick the dominant source language by file
  extension counts. Used for the agent's per-language prompt hint.
- `ticket_skip_reason(pr, diff)` — return a short string when the PR
  shouldn't be reviewed (fork, bot-authored, trivial diff, too large).
  None for "review it." First-match-wins.
- `is_skip_path(path)` — proxy to `domain/intake/parsing.is_skippable_path`
  so reviewer code doesn't reach into intake internals directly.
"""

from __future__ import annotations

from typing import Any

from app.domain.vcs import Diff


def is_skip_path(path: str) -> bool:
    """Return True if `path` matches the intake-side skip list (vendored
    deps, generated files, lockfiles, etc.)."""
    # Deferred import keeps the module-import path light + avoids any
    # circular-import risk with intake → reviewer at boot.
    from app.domain.intake import is_skippable_path  # noqa: PLC0415

    return is_skippable_path(path)


def ticket_skip_reason(pr: Any, diff: Diff) -> str | None:
    """First-match-wins admission for a PR + diff pair. Returns a short
    reason string when the PR should be skipped, None when it advances.

    Reasons (in order):
    - `fork`: PR opened from a fork (don't run agent on untrusted code).
    - `bot_author`: PR author is a bot account (likely automated PR).
    - `trivial_diff`: every changed file matches the intake skip list.
    - `too_large`: total added+deleted lines exceeds 5000.
    """
    if pr.is_fork:
        return "fork"
    if pr.author_type == "bot":
        return "bot_author"
    if diff.files and all(is_skip_path(f.path) for f in diff.files):
        return "trivial_diff"
    total_lines = sum(f.additions + f.deletions for f in diff.files)
    if total_lines > 5000:
        return "too_large"
    return None


# Extension → language map used by the dominant-language heuristic. Order
# matters only insofar as ".tsx" must precede ".ts" wouldn't be reached
# from the "all extensions" loop — we test full extensions one at a time.
_EXT_TO_LANG: dict[str, str] = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".java": "Java",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".c": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".h": "C/C++",
}


def detect_language(diff: Any) -> str | None:
    """Pick the dominant language by file-extension counts in `diff.files`.
    Returns None when none of the recognized extensions appear (so the
    agent prompt falls back to its language-agnostic header)."""
    counts: dict[str, int] = {}
    for f in diff.files:
        for ext, lang in _EXT_TO_LANG.items():
            if f.path.lower().endswith(ext):
                counts[lang] = counts.get(lang, 0) + 1
                break
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


__all__ = [
    "detect_language",
    "is_skip_path",
    "ticket_skip_reason",
]
