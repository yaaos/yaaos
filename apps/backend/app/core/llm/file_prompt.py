"""`FilePrompt` — parsed `.prompt.md` file with jinja2-templated messages.

One file = one prompt. YAML frontmatter carries `name`, `version`, `model`,
plus any model params (`temperature`, `max_tokens`, ...). Body is a jinja2
template split into messages by `<system>`, `<user>`, `<assistant>` markers.

Rendering happens at `ainvoke` time — we render templates with the input dict
*before* LangChain sees the messages, so we never mix jinja2 with LangChain's
own templating engine.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from jinja2 import StrictUndefined, Template
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.core.llm.exceptions import PromptParseError

Role = Literal["system", "user", "assistant"]

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_MESSAGE_TAG_RE = re.compile(r"<(system|user|assistant)>(.*?)</\1>", re.DOTALL)
_REQUIRED_FRONTMATTER = ("name", "version", "model")


@dataclass(frozen=True)
class ParsedMessage:
    """One message slot in a `FilePrompt`. Body is a raw jinja2 template."""

    role: Role
    template: str

    def render(self, input_vars: Mapping[str, Any]) -> BaseMessage:
        text = Template(self.template, undefined=StrictUndefined).render(**input_vars).strip()
        match self.role:
            case "system":
                return SystemMessage(content=text)
            case "user":
                return HumanMessage(content=text)
            case "assistant":
                return AIMessage(content=text)


@dataclass(frozen=True)
class FilePrompt:
    """Immutable parsed prompt. No I/O after construction."""

    name: str
    version: int
    model: str
    model_params: dict[str, Any] = field(default_factory=dict)
    messages: tuple[ParsedMessage, ...] = ()
    source_path: Path | None = None

    def render(self, input_vars: Mapping[str, Any]) -> list[BaseMessage]:
        return [m.render(input_vars) for m in self.messages]

    @property
    def tag(self) -> str:
        """Per-prompt identifier the gateway groups by (e.g. `classify_reply.v1`)."""
        return f"{self.name}.v{self.version}"


def load_prompt(path: Path) -> FilePrompt:
    """Parse a `.prompt.md` file. Path is explicit — no stack inspection."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        raise PromptParseError(f"{path}: missing YAML frontmatter (--- … ---)")

    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        raise PromptParseError(f"{path}: frontmatter is not valid YAML: {e}") from e
    if not isinstance(front, dict):
        raise PromptParseError(f"{path}: frontmatter must be a mapping")

    missing = [k for k in _REQUIRED_FRONTMATTER if k not in front]
    if missing:
        raise PromptParseError(f"{path}: frontmatter missing required keys: {missing}")

    name = str(front.pop("name"))
    version = int(front.pop("version"))
    model = str(front.pop("model"))
    model_params = dict(front)  # remainder is passed through to init_chat_model

    body = match.group(2)
    messages = tuple(
        ParsedMessage(role=role, template=text.strip()) for role, text in _MESSAGE_TAG_RE.findall(body)
    )
    if not messages:
        raise PromptParseError(f"{path}: no <system>/<user>/<assistant> message blocks found")

    return FilePrompt(
        name=name,
        version=version,
        model=model,
        model_params=model_params,
        messages=messages,
        source_path=path,
    )
