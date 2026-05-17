"""Installs yaaos reviewer subagent definitions into `$HOME/.claude/agents/`.

Subagent *content* is plugin-agnostic and lives in
`app/domain/coding_agent/reviewers/*.md`. This module owns the Claude-Code-
specific packaging: prepending YAML frontmatter (name, description) and
writing to the location Claude Code's CLI reads from.

Called once per workspace provision (idempotent — safe to re-run; the file
contents are deterministic). Per-workspace install is forward-compatible
with M02+ Docker workspaces where each workspace has its own HOME.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

log = structlog.get_logger("claude_code.installer")


# Reviewer file → (subagent name, description). Description is the trigger
# Claude Code uses to decide when to invoke the subagent — keep it specific.
_REVIEWERS: dict[str, tuple[str, str]] = {
    "architecture.md": (
        "yaaos-architecture",
        "Reviews module boundaries, patterns, abstractions, and CLAUDE.md adherence in PR diffs.",
    ),
    "security.md": (
        "yaaos-security",
        "Reviews PR diffs for auth, injection, secret handling, and crypto misuse.",
    ),
    "line-level.md": (
        "yaaos-line-level",
        "Reviews PR diffs for per-line correctness, idioms, and code-level patterns (e.g., no mocks in tests).",
    ),
    "tests.md": (
        "yaaos-tests",
        "Reviews PR diffs for test presence and quality of new behavior.",
    ),
    "docs.md": (
        "yaaos-docs",
        "Reviews PR diffs for documentation sync — every change should update relevant docs in the same PR.",
    ),
    "skill.md": (
        "yaaos-skill",
        "Reviews Claude Code Skill files for trigger quality, structure, and clarity. Invoke only when the diff touches **/SKILL.md or .claude/skills/**.",
    ),
}


def _reviewers_dir() -> Path:
    """Return the path to the bundled reviewer markdown directory."""
    return Path(__file__).resolve().parent.parent.parent / "domain" / "coding_agent" / "reviewers"


def _agents_install_dir() -> Path:
    return Path(os.path.expanduser("~/.claude/agents"))


def _wrap_with_frontmatter(name: str, description: str, body: str) -> str:
    """Prepend Claude Code's YAML frontmatter to the plugin-agnostic body."""
    return f"""---
name: {name}
description: {description}
---

{body.lstrip()}"""


def install_subagents() -> int:
    """Install all yaaos-* subagent definitions into `$HOME/.claude/agents/`.

    Idempotent: overwrites any existing yaaos-prefixed file with the bundled
    content. Returns the number of files written. Other (non-yaaos) files in
    the target directory are left untouched.
    """
    src_dir = _reviewers_dir()
    dst_dir = _agents_install_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for filename, (subagent_name, description) in _REVIEWERS.items():
        src = src_dir / filename
        if not src.is_file():
            log.warning("claude_code.installer.source_missing", path=str(src))
            continue
        body = src.read_text(encoding="utf-8")
        dst = dst_dir / f"{subagent_name}.md"
        dst.write_text(_wrap_with_frontmatter(subagent_name, description, body), encoding="utf-8")
        written += 1

    log.info(
        "claude_code.subagents_installed",
        count=written,
        install_dir=str(dst_dir),
    )
    return written
