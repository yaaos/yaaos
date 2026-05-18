"""Re-review command parsing — pure helper.

Plan/notes/full-pr-flow.md §9.5 / §13 step 13 — canonical PR-comment command
set: `@yaaos review` (incremental), `@yaaos full review`, `@yaaos cancel`. The
legacy `@yaaos rereview` form maps to `full review` for backward compat. Plus
`confirm` as a bare body matches the mid-band acknowledgment confirmation
path (§6.4 step 4).
"""

import re

# Match the longest forms first so `full review` doesn't accidentally
# parse as `review` + trailing junk.
_YAAOS_FULL_REVIEW_RE = re.compile(r"@yaaos\s+full\s+review\b", re.IGNORECASE)
_YAAOS_REVIEW_RE = re.compile(r"@yaaos\s+review\b", re.IGNORECASE)
_YAAOS_CANCEL_RE = re.compile(r"@yaaos\s+cancel\b", re.IGNORECASE)
# Legacy: @yaaos rereview (with optional -<specialty>) — keep accepting; it
# maps to `full review`.
_LEGACY_REREVIEW_RE = re.compile(r"@yaaos(?:-[a-z0-9-]+)?\s+rereview\b", re.IGNORECASE)


def parse_yaaos_command(body: str) -> str | None:
    """Returns `'review' | 'full review' | 'cancel'` or `None`."""
    s = body or ""
    if _YAAOS_FULL_REVIEW_RE.search(s):
        return "full review"
    if _LEGACY_REREVIEW_RE.search(s):
        return "full review"
    if _YAAOS_REVIEW_RE.search(s):
        return "review"
    if _YAAOS_CANCEL_RE.search(s):
        return "cancel"
    return None


def parse_rereview(body: str) -> tuple[bool, None]:
    """Legacy `@yaaos rereview` parser. Matches the OLD vocabulary only.

    Returns (matched, None). The new vocabulary (`@yaaos review`,
    `@yaaos full review`, `@yaaos cancel`) is recognized by
    `parse_yaaos_command` — callers that want to honor both run
    `parse_rereview` first (legacy intent: re-review) then
    `parse_yaaos_command` for the new commands.
    """
    return bool(_LEGACY_REREVIEW_RE.search(body or "")), None


def is_mid_band_confirm(body: str) -> bool:
    """Plan §6.4 step 4 mid-band path: developer types `confirm` to acknowledge.

    Bare `confirm` on its own line (case-insensitive, optional surrounding
    whitespace / punctuation). Mid-band only fires when a prior yaaos reply
    asked for confirmation; the caller is responsible for checking that.
    """
    s = (body or "").strip().lower()
    return s in {"confirm", "confirm.", "confirmed", "yes confirm"}


# Skip lists for trivial diffs (per requirements.md)
_LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "go.sum",
}
_VENDOR_DIRS = ("node_modules/", "vendor/", "third_party/", "dist/", "build/", "out/")
_GENERATED_SUFFIXES = (".pb.go", ".gen.go", ".gen.ts", ".gen.js")
_BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".woff",
    ".woff2",
    ".ttf",
}


def is_skippable_path(path: str) -> bool:
    """True if the path should be excluded from agent review."""
    name = path.rsplit("/", 1)[-1]
    if name in _LOCKFILES:
        return True
    for prefix in _VENDOR_DIRS:
        if path.startswith(prefix) or f"/{prefix}" in path:
            return True
    if "_generated" in name:
        return True
    for suffix in _GENERATED_SUFFIXES:
        if path.endswith(suffix):
            return True
    for ext in _BINARY_EXTS:
        if path.lower().endswith(ext):
            return True
    return False
