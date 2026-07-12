"""Skills-bundle builder for per-vendor coding-agent downloads.

Reads the canonical `.claude/` skill and agent sources from the configured
`yaaos_skills_source_dir`, parses each file's YAML frontmatter, delegates
rendering to the plugin's `render_skill_bundle` method, and assembles the
result into an in-memory ZIP archive.

Filesystem access here is a sanctioned carve-out — see
`apps/backend/docs/patterns.md § Filesystem + processes via core/workspace`
for the rationale and the full exception list. Do not extend this module for
arbitrary filesystem access.
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import yaml

from app.core.coding_agent.service import get_plugin
from app.core.coding_agent.types import AgentSource, BundleFile, CodingAgentPlugin, SkillSource
from app.core.config import get_settings

# Pattern matching a YAML frontmatter block at the start of a markdown file.
# Groups: (1) frontmatter YAML, (2) body after the closing ---
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n(.*)\Z", re.DOTALL)


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body string) from a markdown file's content.

    Both halves are stripped of leading/trailing whitespace. When no
    frontmatter block is present, returns an empty dict and the full content.
    YAML parse errors yield an empty dict rather than raising.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content.strip()
    raw_yaml = m.group(1)
    body = m.group(2).strip()
    try:
        fm = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, body


def _reconstruct_md(frontmatter: dict[str, Any], body: str) -> str:
    """Reconstruct a markdown file with YAML frontmatter from parsed parts."""
    if frontmatter:
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).rstrip()
        return f"---\n{yaml_text}\n---\n\n{body}\n"
    return f"{body}\n"


def _load_skill_sources(skills_dir: Path) -> list[SkillSource]:
    """Load all `pipeline-*` skill directories from `skills_dir`.

    Each skill directory must contain a `SKILL.md` (or, in the one exception,
    be the `pipeline-schemas` directory which may carry only `.schema.json`
    files). Extra non-`SKILL.md` files in the directory are included as
    ``extra_files`` with their repo-root-relative paths.

    The skills_dir is assumed to be `{source_dir}/skills`.
    source_dir is the parent used to compute repo-root-relative paths.
    """
    sources: list[SkillSource] = []
    if not skills_dir.is_dir():
        raise FileNotFoundError(f"Skills source directory not found: {skills_dir}")

    source_dir = skills_dir.parent

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("pipeline-"):
            continue

        skill_md_path = entry / "SKILL.md"
        if not skill_md_path.exists():
            # Only pipeline-schemas is exempt (schema-only, no SKILL.md).
            if entry.name != "pipeline-schemas":
                continue
            frontmatter: dict[str, Any] = {}
            body = ""
        else:
            raw = skill_md_path.read_text(encoding="utf-8")
            frontmatter, body = _parse_frontmatter(raw)

        # Collect extra files (anything that isn't SKILL.md).
        extra: list[BundleFile] = []
        for file_path in sorted(entry.rglob("*")):
            if not file_path.is_file() or file_path.name == "SKILL.md":
                continue
            rel = file_path.relative_to(source_dir).as_posix()
            extra.append(BundleFile(path=rel, content=file_path.read_text(encoding="utf-8")))

        name: str = frontmatter.get("name") or entry.name
        sources.append(
            SkillSource(
                name=name,
                frontmatter=frontmatter,
                body=body,
                extra_files=tuple(extra),
            )
        )

    return sources


def _load_agent_sources(agents_dir: Path) -> list[AgentSource]:
    """Load all `pipeline-*.md` agent files from `agents_dir`.

    ``agents_dir`` is assumed to be ``{source_dir}/agents``.
    """
    sources: list[AgentSource] = []
    if not agents_dir.is_dir():
        raise FileNotFoundError(f"Agents source directory not found: {agents_dir}")

    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_file():
            continue
        if not (entry.name.startswith("pipeline-") and entry.name.endswith(".md")):
            continue

        raw = entry.read_text(encoding="utf-8")
        frontmatter, body = _parse_frontmatter(raw)
        name: str = frontmatter.get("name") or entry.stem
        sources.append(AgentSource(name=name, frontmatter=frontmatter, body=body))

    return sources


def _build_bundle_sync(plugin: CodingAgentPlugin, source_dir: Path) -> bytes:
    """Synchronous core of `build_skills_bundle_zip` — runs inside `to_thread`.

    Loads sources, renders via the plugin, and assembles the ZIP archive.
    All file I/O is blocking; this function must not be called directly on
    the async event loop.
    """
    skills_dir = source_dir / "skills"
    agents_dir = source_dir / "agents"

    skill_sources = _load_skill_sources(skills_dir)
    agent_sources = _load_agent_sources(agents_dir)

    bundle_files = plugin.render_skill_bundle(skill_sources, agent_sources)

    buf = io.BytesIO()
    # Fixed modification time so the archive bytes are reproducible across
    # runs with identical source content (same rationale as the JS builder).
    fixed_mtime = (2020, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for bf in sorted(bundle_files, key=lambda f: f.path):
            zi = zipfile.ZipInfo(filename=bf.path, date_time=fixed_mtime)
            zf.writestr(zi, bf.content)
    return buf.getvalue()


async def build_skills_bundle_zip(plugin_id: str) -> bytes:
    """Build an in-memory ZIP of the skills bundle for the named plugin.

    Loads the canonical `.claude/skills/pipeline-*/**` and
    `.claude/agents/pipeline-*.md` sources from `settings.yaaos_skills_source_dir`,
    parses their YAML frontmatter, passes the parsed objects to the plugin's
    `render_skill_bundle` method, and packages the output into a ZIP archive.

    File I/O runs in a thread pool via `asyncio.to_thread` so the event loop
    is not blocked.

    Raises:
        PluginNotFoundError: when ``plugin_id`` is not registered (→ 404).
        FileNotFoundError: when the skills source directory is missing from the
            image — a deploy defect (→ 500).
    """
    plugin = get_plugin(plugin_id)
    settings = get_settings()
    source_dir = Path(settings.yaaos_skills_source_dir)
    return await asyncio.to_thread(_build_bundle_sync, plugin, source_dir)
