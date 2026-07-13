"""Unit + service tests for `core/coding_agent.build_skills_bundle_zip` and
the per-vendor `render_skill_bundle` implementations.

Unit tier:
  - `_parse_frontmatter` parses YAML header correctly.
  - `_load_skill_sources` builds `SkillSource` objects from a tmp-dir tree.
  - `_load_agent_sources` builds `AgentSource` objects from a tmp-dir tree.
  - `ClaudeCodePlugin.render_skill_bundle` passthrough — output paths == input names.
  - `CodexPlugin.render_skill_bundle` — emits `.codex/agents/*.toml` + `AGENTS.md`
    with the delegation-authorization sentence.

Service tier (`@pytest.mark.service`):
  - Bundle endpoint returns 200 + a valid ZIP for both plugins.
  - Entry list matches `render_skill_bundle` output.
  - 404 on an unknown plugin id.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.core.coding_agent.skills_bundle import (
    _get_shipped_skill_version_sync,
    _load_agent_sources,
    _load_skill_sources,
    _parse_frontmatter,
    build_skills_bundle_zip,
)

# ── Frontmatter parsing ───────────────────────────────────────────────────────


def test_parse_frontmatter_with_yaml() -> None:
    content = "---\nname: my-skill\neffort: high\n---\n\n# Body here\nsome text\n"
    fm, body = _parse_frontmatter(content)
    assert fm == {"name": "my-skill", "effort": "high"}
    assert "Body here" in body


def test_parse_frontmatter_no_frontmatter() -> None:
    content = "# Just a body\nno yaml here"
    fm, body = _parse_frontmatter(content)
    assert fm == {}
    assert "Just a body" in body


def test_parse_frontmatter_empty_yaml_block() -> None:
    content = "---\n\n---\n\nbody"
    fm, body = _parse_frontmatter(content)
    assert fm == {}
    assert body == "body"


# ── Source loading ────────────────────────────────────────────────────────────


def _make_skill_tree(root: Path) -> None:
    """Build a minimal `.claude/`-style skill tree under ``root``."""
    skills_dir = root / "skills"
    for name, fm_extra in [
        ("pipeline-implement", {"description": "Implement skill"}),
        ("pipeline-code-review", {"description": "Code review skill"}),
    ]:
        d = skills_dir / name
        d.mkdir(parents=True)
        skill_md = d / "SKILL.md"
        skill_md.write_text(
            f"---\nname: {name}\ndescription: {fm_extra['description']}\n---\n\n# {name}\nBody.\n",
            encoding="utf-8",
        )
    # pipeline-schemas: no SKILL.md, only a schema file.
    schemas_dir = skills_dir / "pipeline-schemas"
    schemas_dir.mkdir(parents=True)
    (schemas_dir / "result.schema.json").write_text('{"type": "object"}', encoding="utf-8")
    # Non-pipeline skill — must be ignored.
    other = skills_dir / "dev-quick"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: dev-quick\n---\n\nBody.\n", encoding="utf-8")


def _make_agent_tree(root: Path) -> None:
    """Build a minimal `.claude/`-style agents tree under ``root``."""
    agents_dir = root / "agents"
    agents_dir.mkdir(parents=True)
    for name in ["pipeline-implement-phase", "pipeline-code-review-phase"]:
        (agents_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: Agent for {name}\n---\n\n# {name}\nDo the work.\n",
            encoding="utf-8",
        )
    # Non-pipeline agent — must be ignored.
    (agents_dir / "dev-helper.md").write_text("---\nname: dev-helper\n---\n\nHelper.\n", encoding="utf-8")


def test_load_skill_sources(tmp_path: Path) -> None:
    _make_skill_tree(tmp_path)
    sources = _load_skill_sources(tmp_path / "skills")
    names = [s.name for s in sources]
    # Only pipeline-* dirs are included; dev-quick is excluded.
    assert "pipeline-implement" in names
    assert "pipeline-code-review" in names
    assert "pipeline-schemas" in names
    assert "dev-quick" not in names
    # Verify frontmatter parsing
    impl = next(s for s in sources if s.name == "pipeline-implement")
    assert impl.frontmatter.get("description") == "Implement skill"
    assert "Body." in impl.body
    # pipeline-schemas: schema.json lands in extra_files
    schemas = next(s for s in sources if s.name == "pipeline-schemas")
    extra_names = [bf.path for bf in schemas.extra_files]
    assert any("result.schema.json" in p for p in extra_names)


def test_load_agent_sources(tmp_path: Path) -> None:
    _make_agent_tree(tmp_path)
    sources = _load_agent_sources(tmp_path / "agents")
    names = [a.name for a in sources]
    assert "pipeline-implement-phase" in names
    assert "pipeline-code-review-phase" in names
    assert "dev-helper" not in names
    agent = next(a for a in sources if a.name == "pipeline-implement-phase")
    assert agent.frontmatter.get("description") == "Agent for pipeline-implement-phase"
    assert "Do the work." in agent.body


# ── ClaudeCodePlugin.render_skill_bundle (passthrough) ───────────────────────


def test_claude_render_skill_bundle_passthrough(tmp_path: Path) -> None:
    """Claude renderer re-emits the canonical .claude/ tree unchanged."""
    from app.plugins.claude_code import ClaudeCodePlugin  # noqa: PLC0415

    plugin = ClaudeCodePlugin()
    _make_skill_tree(tmp_path)
    _make_agent_tree(tmp_path)
    skills = _load_skill_sources(tmp_path / "skills")
    agents = _load_agent_sources(tmp_path / "agents")

    files = plugin.render_skill_bundle(skills, agents)
    paths = {f.path for f in files}

    # Skill files live under .claude/skills/
    assert ".claude/skills/pipeline-implement/SKILL.md" in paths
    assert ".claude/skills/pipeline-code-review/SKILL.md" in paths
    # Agent files live under .claude/agents/
    assert ".claude/agents/pipeline-implement-phase.md" in paths
    assert ".claude/agents/pipeline-code-review-phase.md" in paths
    # No .codex/ paths; no AGENTS.md
    assert not any(p.startswith(".codex/") for p in paths)
    assert "AGENTS.md" not in paths

    # Passthrough: SKILL.md content contains the original body text
    impl_file = next(f for f in files if f.path == ".claude/skills/pipeline-implement/SKILL.md")
    assert "pipeline-implement" in impl_file.content


# ── CodexPlugin.render_skill_bundle ──────────────────────────────────────────


def test_codex_render_skill_bundle_paths(tmp_path: Path) -> None:
    """Codex renderer emits .codex/skills/, .codex/agents/*.toml, and AGENTS.md."""
    from app.plugins.codex import CodexPlugin  # noqa: PLC0415

    plugin = CodexPlugin()
    _make_skill_tree(tmp_path)
    _make_agent_tree(tmp_path)
    skills = _load_skill_sources(tmp_path / "skills")
    agents = _load_agent_sources(tmp_path / "agents")

    files = plugin.render_skill_bundle(skills, agents)
    paths = {f.path for f in files}

    # Skills under .codex/
    assert ".codex/skills/pipeline-implement/SKILL.md" in paths
    assert ".codex/skills/pipeline-code-review/SKILL.md" in paths
    # Agent TOMLs
    assert ".codex/agents/pipeline-implement-phase.toml" in paths
    assert ".codex/agents/pipeline-code-review-phase.toml" in paths
    # AGENTS.md at repo root
    assert "AGENTS.md" in paths
    # No .claude/ paths
    assert not any(p.startswith(".claude/") for p in paths)


def test_codex_agents_md_authorization_sentence(tmp_path: Path) -> None:
    """AGENTS.md must contain the delegation-authorization trigger vocabulary."""
    from app.plugins.codex import CodexPlugin  # noqa: PLC0415

    plugin = CodexPlugin()
    _make_skill_tree(tmp_path)
    _make_agent_tree(tmp_path)
    skills = _load_skill_sources(tmp_path / "skills")
    agents = _load_agent_sources(tmp_path / "agents")

    files = plugin.render_skill_bundle(skills, agents)
    agents_md = next(f for f in files if f.path == "AGENTS.md")

    # Must use the exact trigger vocabulary (per requirements Notes / codex PR #30274/#30511).
    content = agents_md.content.lower()
    assert "sub-agents" in content or "sub-agent" in content
    assert "delegation" in content
    assert "parallel agent work" in content


def test_codex_agent_toml_defensive_restatement(tmp_path: Path) -> None:
    """Each agent TOML must include the defensive restatement directive."""
    from app.plugins.codex import CodexPlugin  # noqa: PLC0415

    plugin = CodexPlugin()
    _make_skill_tree(tmp_path)
    _make_agent_tree(tmp_path)
    skills = _load_skill_sources(tmp_path / "skills")
    agents = _load_agent_sources(tmp_path / "agents")

    files = plugin.render_skill_bundle(skills, agents)
    toml_file = next(f for f in files if f.path == ".codex/agents/pipeline-implement-phase.toml")

    # Defensive restatement phrase must appear in the TOML content
    assert "restate" in toml_file.content.lower()
    assert "deliverable" in toml_file.content.lower()
    # Original body text also present
    assert "Do the work." in toml_file.content


def test_codex_agent_toml_structure(tmp_path: Path) -> None:
    """Agent TOML has expected keys: name, description, [prompt] section."""
    from app.plugins.codex import CodexPlugin  # noqa: PLC0415

    plugin = CodexPlugin()
    _make_skill_tree(tmp_path)
    _make_agent_tree(tmp_path)
    skills = _load_skill_sources(tmp_path / "skills")
    agents = _load_agent_sources(tmp_path / "agents")

    files = plugin.render_skill_bundle(skills, agents)
    toml_file = next(f for f in files if f.path == ".codex/agents/pipeline-implement-phase.toml")

    assert "name = " in toml_file.content
    assert "description = " in toml_file.content
    assert "[prompt]" in toml_file.content
    assert "content" in toml_file.content


# ── Service tests — use isolated registries with real (non-stub) plugins ─────
# build_skills_bundle_zip reads settings + calls plugin.render_skill_bundle.
# We register real plugin instances inside set_coding_agents_for_tests so the
# stub wrapper (active under YAAOS_CODING_AGENT_STUB=1 in the service-test
# environment) doesn't interfere with render_skill_bundle — which IS a
# meaningful method the stub should not intercept.


@pytest.mark.service
async def test_build_skills_bundle_zip_claude(tmp_path: Path) -> None:
    """build_skills_bundle_zip returns valid ZIP bytes for claude_code."""
    import os  # noqa: PLC0415

    from app.core.coding_agent import set_coding_agents_for_tests  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415
    from app.plugins.claude_code import ClaudeCodePlugin  # noqa: PLC0415

    _make_skill_tree(tmp_path)
    _make_agent_tree(tmp_path)

    original = get_settings().yaaos_skills_source_dir
    get_settings.cache_clear()
    os.environ["YAAOS_SKILLS_SOURCE_DIR"] = str(tmp_path)

    try:
        with set_coding_agents_for_tests(scenario="empty") as reg:
            reg.register(ClaudeCodePlugin())
            data = await build_skills_bundle_zip("claude_code")
    finally:
        if original:
            os.environ["YAAOS_SKILLS_SOURCE_DIR"] = original
        else:
            os.environ.pop("YAAOS_SKILLS_SOURCE_DIR", None)
        get_settings.cache_clear()

    assert isinstance(data, bytes) and len(data) > 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert any(".claude/skills/pipeline-implement/SKILL.md" in n for n in names)
    assert any(".claude/agents/pipeline-implement-phase.md" in n for n in names)


@pytest.mark.service
async def test_build_skills_bundle_zip_codex(tmp_path: Path) -> None:
    """build_skills_bundle_zip returns valid ZIP bytes for codex."""
    import os  # noqa: PLC0415

    from app.core.coding_agent import set_coding_agents_for_tests  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415
    from app.plugins.codex import CodexPlugin  # noqa: PLC0415

    _make_skill_tree(tmp_path)
    _make_agent_tree(tmp_path)

    original = get_settings().yaaos_skills_source_dir
    get_settings.cache_clear()
    os.environ["YAAOS_SKILLS_SOURCE_DIR"] = str(tmp_path)

    try:
        with set_coding_agents_for_tests(scenario="empty") as reg:
            reg.register(CodexPlugin())
            data = await build_skills_bundle_zip("codex")
    finally:
        if original:
            os.environ["YAAOS_SKILLS_SOURCE_DIR"] = original
        else:
            os.environ.pop("YAAOS_SKILLS_SOURCE_DIR", None)
        get_settings.cache_clear()

    assert isinstance(data, bytes) and len(data) > 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert any(".codex/skills/pipeline-implement/SKILL.md" in n for n in names)
    assert any(".codex/agents/pipeline-implement-phase.toml" in n for n in names)
    assert "AGENTS.md" in names


@pytest.mark.service
async def test_build_skills_bundle_zip_unknown_plugin(tmp_path: Path) -> None:
    """build_skills_bundle_zip raises PluginNotFoundError for unknown plugin."""
    from app.core.coding_agent import PluginNotFoundError, set_coding_agents_for_tests  # noqa: PLC0415

    with set_coding_agents_for_tests(scenario="empty"):
        with pytest.raises(PluginNotFoundError):
            await build_skills_bundle_zip("no_such_plugin")


# ── get_shipped_skill_version ─────────────────────────────────────────────────


def test_get_shipped_skill_version_returns_version(tmp_path: Path) -> None:
    """Returns the `version` string from the SKILL.md frontmatter."""
    skill_dir = tmp_path / "skills" / "pipeline-foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: pipeline-foo\nversion: '1.2.3'\n---\n\n# Body\n")
    assert _get_shipped_skill_version_sync("pipeline-foo", tmp_path) == "1.2.3"


def test_get_shipped_skill_version_none_for_missing_skill(tmp_path: Path) -> None:
    """Returns None when the skill directory does not exist."""
    assert _get_shipped_skill_version_sync("pipeline-nonexistent", tmp_path) is None


def test_get_shipped_skill_version_none_when_no_version_key(tmp_path: Path) -> None:
    """Returns None when the SKILL.md has no `version` key."""
    skill_dir = tmp_path / "skills" / "pipeline-bar"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: pipeline-bar\n---\n\n# Body\n")
    assert _get_shipped_skill_version_sync("pipeline-bar", tmp_path) is None


def test_get_shipped_skill_version_none_for_missing_skill_md(tmp_path: Path) -> None:
    """Returns None when the skill directory exists but SKILL.md is absent."""
    skill_dir = tmp_path / "skills" / "pipeline-baz"
    skill_dir.mkdir(parents=True)
    assert _get_shipped_skill_version_sync("pipeline-baz", tmp_path) is None


def test_get_shipped_skill_version_returns_none_for_non_string_version(tmp_path: Path) -> None:
    """Returns None when `version` is present but is not a string (e.g. int)."""
    skill_dir = tmp_path / "skills" / "pipeline-qux"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: pipeline-qux\nversion: 1\n---\n\n# Body\n")
    assert _get_shipped_skill_version_sync("pipeline-qux", tmp_path) is None
