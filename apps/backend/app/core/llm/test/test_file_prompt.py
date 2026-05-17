"""Tests for `FilePrompt` parsing + rendering. No LangChain in the loop."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from jinja2 import UndefinedError
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import FilePrompt, PromptParseError, load_prompt
from app.core.llm.file_prompt import ParsedMessage

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_load_prompt_parses_frontmatter_and_messages() -> None:
    prompt = load_prompt(FIXTURE_DIR / "example.prompt.md")

    assert prompt.name == "example"
    assert prompt.version == 1
    assert prompt.model == "anthropic:claude-haiku-4-5"
    assert prompt.model_params == {"temperature": 0.1, "max_tokens": 256}
    assert prompt.tag == "example.v1"
    assert len(prompt.messages) == 2
    assert prompt.messages[0].role == "system"
    assert prompt.messages[1].role == "user"


def test_render_returns_langchain_messages_with_input_vars() -> None:
    prompt = load_prompt(FIXTURE_DIR / "example.prompt.md")

    rendered = prompt.render({"subject": "the migration"})

    assert isinstance(rendered[0], SystemMessage)
    assert isinstance(rendered[1], HumanMessage)
    assert "the migration" in rendered[1].content


def test_render_with_missing_input_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.prompt.md"
    p.write_text(
        "---\nname: x\nversion: 1\nmodel: anthropic:claude-haiku-4-5\n---\n<user>{{ required_var }}</user>\n"
    )
    prompt = load_prompt(p)

    with pytest.raises(UndefinedError):
        prompt.render({})


def test_missing_frontmatter_raises(tmp_path: Path) -> None:
    p = tmp_path / "no_frontmatter.prompt.md"
    p.write_text("<user>hi</user>")

    with pytest.raises(PromptParseError, match="missing YAML frontmatter"):
        load_prompt(p)


def test_missing_required_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "missing_keys.prompt.md"
    p.write_text("---\nname: x\nversion: 1\n---\n<user>hi</user>")

    with pytest.raises(PromptParseError, match="missing required keys"):
        load_prompt(p)


def test_no_message_blocks_raises(tmp_path: Path) -> None:
    p = tmp_path / "no_messages.prompt.md"
    p.write_text("---\nname: x\nversion: 1\nmodel: anthropic:claude-haiku-4-5\n---\njust prose\n")

    with pytest.raises(PromptParseError, match="no <system>/<user>/<assistant>"):
        load_prompt(p)


def test_parsed_message_render_strips_whitespace() -> None:
    msg = ParsedMessage(role="user", template="  hello {{ name }}  \n")

    rendered = msg.render({"name": "world"})

    assert rendered.content == "hello world"


def test_file_prompt_is_immutable() -> None:
    prompt = FilePrompt(
        name="x",
        version=1,
        model="anthropic:claude-haiku-4-5",
        messages=(ParsedMessage(role="user", template="hi"),),
    )

    with pytest.raises(FrozenInstanceError):
        prompt.name = "y"  # type: ignore[misc]
