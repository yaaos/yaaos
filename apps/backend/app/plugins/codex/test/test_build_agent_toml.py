"""Unit tests for `_build_agent_toml` — TOML generation for `.codex/agents/*.toml`.

Covers the basic multi-line string encoding, including single-quote pass-through
and backslash/double-quote escaping.
"""

from __future__ import annotations

import tomllib

from app.core.coding_agent import AgentSource
from app.plugins.codex.service import _build_agent_toml


def _make_agent(name: str, body: str, description: str = "") -> AgentSource:
    return AgentSource(
        name=name,
        frontmatter={"name": name, "description": description},
        body=body,
    )


def _parse_toml(content: str) -> dict:
    return tomllib.loads(content)


def test_basic_agent_toml_roundtrip() -> None:
    """A simple body with no special characters survives TOML round-trip."""
    agent = _make_agent("pipeline-reviewer", "Do a code review.", description="Reviews code")
    toml_text = _build_agent_toml(agent)
    parsed = _parse_toml(toml_text)
    assert parsed["name"] == "pipeline-reviewer"
    assert parsed["description"] == "Reviews code"
    assert "Do a code review." in parsed["prompt"]["content"]


def test_single_quotes_pass_through() -> None:
    """Single quotes in the body are embedded verbatim (not escaped)."""
    body = "It's a test with 'single quotes' and even '''triple single quotes'''."
    agent = _make_agent("test-agent", body)
    toml_text = _build_agent_toml(agent)
    parsed = _parse_toml(toml_text)
    assert "'''triple single quotes'''" in parsed["prompt"]["content"]


def test_double_quotes_escaped() -> None:
    """Double quotes in the body are escaped so they don't terminate the multi-line string."""
    body = 'He said "hello" and she said "goodbye".'
    agent = _make_agent("test-agent", body)
    toml_text = _build_agent_toml(agent)
    parsed = _parse_toml(toml_text)
    assert parsed["prompt"]["content"].strip().endswith('He said "hello" and she said "goodbye".')


def test_backslash_escaped() -> None:
    """Backslashes in the body are escaped so they survive TOML round-trip."""
    body = r"Path is C:\Users\test\file.txt"
    agent = _make_agent("test-agent", body)
    toml_text = _build_agent_toml(agent)
    parsed = _parse_toml(toml_text)
    assert r"C:\Users\test\file.txt" in parsed["prompt"]["content"]


def test_triple_double_quotes_escaped() -> None:
    """Triple double-quotes in the body are safe (each \" is escaped individually)."""
    body = 'Body with """triple double quotes""".'
    agent = _make_agent("test-agent", body)
    toml_text = _build_agent_toml(agent)
    parsed = _parse_toml(toml_text)
    assert '"""triple double quotes"""' in parsed["prompt"]["content"]


def test_defensive_restatement_prepended() -> None:
    """The defensive restatement directive is prepended to every agent body."""
    body = "Actual instructions here."
    agent = _make_agent("test-agent", body)
    toml_text = _build_agent_toml(agent)
    parsed = _parse_toml(toml_text)
    content = parsed["prompt"]["content"]
    assert "restate" in content.lower()
    assert "Actual instructions here." in content
