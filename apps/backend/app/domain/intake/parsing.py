"""Re-review command parsing — pure helper."""

import re

# The reviewer is now a single parent agent; specialty selection happens
# inside the agent's own subagent dispatch. The legacy `@yaaos-<specialty>`
# form is still accepted but the specialty is ignored.
_REREVIEW_RE = re.compile(
    r"@yaaos(?:-[a-z0-9-]+)?\s+rereview",
    re.IGNORECASE,
)


def parse_rereview(body: str) -> tuple[bool, None]:
    """Returns (matched, None). Specialty is ignored — one reviewer per ticket."""
    return bool(_REREVIEW_RE.search(body or "")), None


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
