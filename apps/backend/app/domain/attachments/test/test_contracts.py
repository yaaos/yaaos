"""Unit tests for parse_frontmatter — pure function, no fixtures needed."""

from __future__ import annotations

from textwrap import dedent

from app.domain.attachments.contracts import ArtifactFrontmatter, parse_frontmatter

_VALID_BLOCK = dedent(
    """\
    ---
    yaaos_artifact_version: 1
    skill: pipeline-requirements
    skill_version: "1.0.0"
    artifact_type: requirements
    produced_at: "2024-01-15T10:00:00Z"
    repo_commit: abc1234
    ---
    # Requirements document body here.
    """
)

_VALID_MINIMAL = dedent(
    """\
    ---
    yaaos_artifact_version: 1
    skill: pipeline-architecture
    skill_version: "2.0.0"
    artifact_type: architecture
    produced_at: "2024-06-01T00:00:00Z"
    ---
    """
)


def test_valid_frontmatter_parsed() -> None:
    result = parse_frontmatter(_VALID_BLOCK)
    assert isinstance(result, ArtifactFrontmatter)
    assert result.yaaos_artifact_version == 1
    assert result.skill == "pipeline-requirements"
    assert result.skill_version == "1.0.0"
    assert result.artifact_type == "requirements"
    assert result.repo_commit == "abc1234"
    assert result.produced_from is None


def test_valid_minimal_no_optional_fields() -> None:
    result = parse_frontmatter(_VALID_MINIMAL)
    assert isinstance(result, ArtifactFrontmatter)
    assert result.repo_commit is None
    assert result.produced_from is None


def test_produced_from_field_parsed() -> None:
    body = dedent(
        """\
        ---
        yaaos_artifact_version: 1
        skill: pipeline-plan
        skill_version: "1.0.0"
        artifact_type: plan
        produced_at: "2024-01-01T00:00:00Z"
        produced_from: "requirements-artifact-id-xyz"
        ---
        body
        """
    )
    result = parse_frontmatter(body)
    assert result is not None
    assert result.produced_from == "requirements-artifact-id-xyz"


def test_no_frontmatter_returns_none() -> None:
    body = "# This is just a plain document with no frontmatter."
    assert parse_frontmatter(body) is None


def test_empty_string_returns_none() -> None:
    assert parse_frontmatter("") is None


def test_malformed_yaml_returns_none() -> None:
    body = dedent(
        """\
        ---
        yaaos_artifact_version: 1
        skill: [unclosed bracket
        ---
        body
        """
    )
    assert parse_frontmatter(body) is None


def test_unknown_field_extra_forbid_returns_none() -> None:
    body = dedent(
        """\
        ---
        yaaos_artifact_version: 1
        skill: pipeline-requirements
        skill_version: "1.0.0"
        artifact_type: requirements
        produced_at: "2024-01-01T00:00:00Z"
        unknown_extra_field: should cause failure
        ---
        body
        """
    )
    assert parse_frontmatter(body) is None


def test_missing_required_field_returns_none() -> None:
    body = dedent(
        """\
        ---
        yaaos_artifact_version: 1
        skill_version: "1.0.0"
        artifact_type: requirements
        produced_at: "2024-01-01T00:00:00Z"
        ---
        body
        """
    )
    # missing 'skill' field
    assert parse_frontmatter(body) is None


def test_frontmatter_not_at_byte_zero_returns_none() -> None:
    body = "\n---\nyaaos_artifact_version: 1\nskill: x\n---\nbody"
    assert parse_frontmatter(body) is None


def test_frontmatter_preceded_by_space_returns_none() -> None:
    body = " ---\nyaaos_artifact_version: 1\nskill: x\n---\nbody"
    assert parse_frontmatter(body) is None


def test_no_closing_fence_returns_none() -> None:
    body = dedent(
        """\
        ---
        yaaos_artifact_version: 1
        skill: pipeline-requirements
        skill_version: "1.0.0"
        artifact_type: requirements
        produced_at: "2024-01-01T00:00:00Z"
        just body with no closing fence
        """
    )
    assert parse_frontmatter(body) is None


def test_wrong_type_for_required_field_returns_none() -> None:
    body = dedent(
        """\
        ---
        yaaos_artifact_version: "not an int"
        skill: pipeline-requirements
        skill_version: "1.0.0"
        artifact_type: requirements
        produced_at: "2024-01-01T00:00:00Z"
        ---
        body
        """
    )
    # yaaos_artifact_version must be int, not a non-numeric string
    # Pydantic v2 coerces strings to int if they are numeric, but "not an int" is not
    assert parse_frontmatter(body) is None
