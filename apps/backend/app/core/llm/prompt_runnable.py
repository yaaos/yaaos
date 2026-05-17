"""`PromptRunnable` — render → invoke → validate, with one retry on malformed.

Wraps `init_chat_model(...).with_structured_output(schema)`. Tags every call
with `f"{prompt.name}.v{prompt.version}"` so the gateway logs per-prompt rows
without span instrumentation. No callback handlers, no header injection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from app.core.llm.exceptions import MalformedOutput
from app.core.llm.file_prompt import FilePrompt

log = structlog.get_logger("core.llm.prompt_runnable")


class PromptRunnable[OutputT: BaseModel]:
    """Single-shot, structured LLM call. Stateless after construction."""

    def __init__(self, prompt: FilePrompt, output_schema: type[OutputT]) -> None:
        self._prompt = prompt
        self._schema = output_schema

    def _build_model(self) -> BaseChatModel:
        # `user` is the OpenAI convention; the Anthropic provider rewrites it
        # into `metadata.user_id`. Either way the gateway logs grouped rows.
        params = {**self._prompt.model_params, "user": self._prompt.tag}
        return init_chat_model(model=self._prompt.model, **params).with_structured_output(
            self._schema, include_raw=False
        )

    async def ainvoke(self, input_vars: Mapping[str, Any]) -> OutputT:
        """Render messages, call the model, validate. One retry on malformed."""
        messages = self._prompt.render(input_vars)
        model = self._build_model()

        try:
            return await self._invoke_once(model, messages)
        except (MalformedOutput, ValidationError) as e:
            log.warning("llm.malformed.retry", prompt=self._prompt.tag, error=str(e))
            try:
                return await self._invoke_once(model, messages)
            except (MalformedOutput, ValidationError) as e2:
                log.error("llm.malformed.dropped", prompt=self._prompt.tag, error=str(e2))
                raise MalformedOutput(f"{self._prompt.tag}: model output failed validation twice") from e2

    async def _invoke_once(self, model: BaseChatModel, messages: list) -> OutputT:
        raw = await model.ainvoke(messages)
        if isinstance(raw, self._schema):
            return raw
        # `with_structured_output` returns the parsed pydantic model directly
        # when include_raw=False; anything else is a contract violation.
        raise MalformedOutput(
            f"{self._prompt.tag}: expected {self._schema.__name__}, got {type(raw).__name__}"
        )
