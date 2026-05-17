"""core/llm — mechanics for direct, single-shot, structured LLM calls.

Owns prompt-file parsing, jinja2 templating, LangChain runnable construction,
structured-output validation, retries, and Braintrust gateway routing. Does
*not* own prompts or schemas — those live in the calling domain module
(e.g. `domain/reviewer/llm/prompts/`).

Code that needs to *look at code* (read files, walk diffs, run searches) goes
through `domain/coding_agent`, not here. core/llm is for text-only reasoning.
"""

from app.core.llm.exceptions import LLMError, MalformedOutput, PromptParseError
from app.core.llm.file_prompt import FilePrompt, ParsedMessage, load_prompt
from app.core.llm.gateway import configure_gateway
from app.core.llm.prompt_runnable import PromptRunnable

__all__ = [
    "FilePrompt",
    "LLMError",
    "MalformedOutput",
    "ParsedMessage",
    "PromptParseError",
    "PromptRunnable",
    "configure_gateway",
    "load_prompt",
]
