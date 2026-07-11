"""Unit tests for `domain/pipelines.render_stage_prompt`.

Pure function — no DB, no subprocess. Tests cover the required-key gate,
the core rendering contract (skill directive, stage header, input section,
artifact-path output directive, JSON schema directive), and the optional
sections (PR block, upstream stages, revision, prior findings).
"""

from __future__ import annotations

import json

import pytest

from app.core.coding_agent import CodingAgentError
from app.domain.pipelines.stage_prompt import render_stage_prompt

_BASE_CTX: dict = {
    "stage_name": "requirements",
    "input": "Add OAuth2 support.",
    "artifact_path": "$TMPDIR/abc123.md",
}

_DIRECTIVE = 'Use the "requirements" skill (.claude/skills/requirements/SKILL.md) to complete this stage.'


def test_missing_required_key_raises() -> None:
    """render_stage_prompt raises CodingAgentError when any required key is absent."""
    for key in ("stage_name", "input", "artifact_path"):
        ctx = {k: v for k, v in _BASE_CTX.items() if k != key}
        with pytest.raises(CodingAgentError, match="missing required keys"):
            render_stage_prompt(ctx, skill_directive=_DIRECTIVE)


def test_skill_directive_is_first_line() -> None:
    """The skill directive must be the first non-empty line of the rendered prompt."""
    result = render_stage_prompt(_BASE_CTX, skill_directive=_DIRECTIVE)
    assert result.startswith(_DIRECTIVE)


def test_stage_name_in_header() -> None:
    """Stage name appears in the rendered header."""
    result = render_stage_prompt(_BASE_CTX, skill_directive=_DIRECTIVE)
    assert "Stage: requirements" in result


def test_input_section_rendered() -> None:
    """Input value appears in the ## Input section."""
    result = render_stage_prompt(_BASE_CTX, skill_directive=_DIRECTIVE)
    assert "Add OAuth2 support." in result


def test_artifact_path_in_output() -> None:
    """Artifact path appears in the ## Output directive."""
    result = render_stage_prompt(_BASE_CTX, skill_directive=_DIRECTIVE)
    assert "$TMPDIR/abc123.md" in result


def test_json_schema_directive_present_by_default() -> None:
    """By default (output_schema_mode='prompt') the strict-JSON directive is appended."""
    ctx = {**_BASE_CTX, "output_schema": {"foo": "bar"}}
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE)
    assert "Your response must start with `{`" in result
    assert '"foo"' in result


def test_native_schema_mode_omits_directive() -> None:
    """output_schema_mode='native' omits the strict-JSON schema directive."""
    ctx = {**_BASE_CTX, "output_schema": {"foo": "bar"}}
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE, output_schema_mode="native")
    assert "Your response must start with" not in result


def test_pr_block_rendered_when_present() -> None:
    """PR fields appear in the output when the context carries a `pr` key."""
    ctx = {
        **_BASE_CTX,
        "pr": {
            "pr_external_id": "PR-42",
            "base_sha": "aaa",
            "head_sha": "bbb",
            "prev_reviewed_head_sha": None,
        },
    }
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE)
    assert "PR-42" in result
    assert "none (first review)" in result


def test_pr_block_absent_when_not_present() -> None:
    """No PR section when the context lacks a `pr` key."""
    result = render_stage_prompt(_BASE_CTX, skill_directive=_DIRECTIVE)
    assert "## Pull request" not in result


def test_revision_section_rendered() -> None:
    """Revision block appears when `revision` key is present."""
    ctx = {
        **_BASE_CTX,
        "revision": {
            "source": "instruction",
            "text": "Add a registration screen.",
            "prior_artifact": "Old plan here.",
        },
    }
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE)
    assert "Revision (Human instruction)" in result
    assert "Add a registration screen." in result
    assert "Old plan here." in result


def test_prior_findings_rendered() -> None:
    """Prior findings appear when the context carries a non-empty `prior_findings` list."""
    ctx = {
        **_BASE_CTX,
        "prior_findings": [
            {
                "finding_id": "F1",
                "severity": "blocker",
                "body": "Missing error handling.",
                "code_file": "src/app.py",
                "code_line": 42,
                "artifact_section": None,
            }
        ],
    }
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE)
    assert "[F1]" in result
    assert "Missing error handling." in result
    assert "src/app.py:42" in result


def test_extra_directives_appended_after_skill_directive() -> None:
    """extra_directives lines appear right after the skill directive."""
    extras = ["Extra line one.", "Extra line two."]
    result = render_stage_prompt(_BASE_CTX, skill_directive=_DIRECTIVE, extra_directives=extras)
    lines = result.splitlines()
    assert lines[0] == _DIRECTIVE
    assert lines[1] == "Extra line one."
    assert lines[2] == "Extra line two."


def test_ticket_id_in_header_when_present() -> None:
    """When `ticket_id` is in the context the header carries it in parentheses."""
    ctx = {**_BASE_CTX, "ticket_id": "T-999"}
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE)
    assert "(ticket T-999)" in result


def test_upstream_stages_section_rendered() -> None:
    """Upstream artifact section is rendered when the context carries `upstream_stages`."""
    ctx = {
        **_BASE_CTX,
        "upstream_stages": [
            {
                "stage_name": "planning",
                "description": "High-level plan.",
                "artifact_body": "Build a widget.",
            }
        ],
    }
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE)
    assert "planning" in result
    assert "Build a widget." in result


def test_output_schema_empty_dict_renders_valid_json() -> None:
    """An empty `output_schema` renders a valid empty-object JSON block."""
    ctx = {**_BASE_CTX, "output_schema": {}}
    result = render_stage_prompt(ctx, skill_directive=_DIRECTIVE)
    # The schema block should contain the serialised empty dict
    assert json.dumps({}, indent=2) in result
