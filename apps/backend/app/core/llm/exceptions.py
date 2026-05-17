"""core/llm exception hierarchy."""

from __future__ import annotations


class LLMError(Exception):
    """Base class for all core/llm failures."""


class MalformedOutput(LLMError):
    """The model returned output that failed schema validation after one retry."""


class PromptParseError(LLMError):
    """The prompt file is missing required frontmatter or has malformed markers."""
