"""`PromptRunnable` — render → invoke → validate, with one retry on malformed.

Wraps `init_chat_model(...).with_structured_output(schema)`. When the
Braintrust gateway is configured (`BRAINTRUST_API_KEY` set in env), every
direct LLM call is transparently routed through it by injecting `base_url`
+ `api_key` into `init_chat_model` — Braintrust's gateway speaks both the
Anthropic and OpenAI wire protocols, so the same call path covers every
provider yaaos uses today. When unset, LangChain falls back to its normal
env-var resolution (direct provider keys).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.llm.exceptions import MalformedOutput
from app.core.llm.file_prompt import FilePrompt

log = structlog.get_logger("core.llm.prompt_runnable")


# Braintrust gateway (the replacement for the deprecated
# `api.braintrust.dev/v1/proxy`). The host is region-independent — the
# gateway dispatches to the right data plane via the BRAINTRUST_API_KEY.
# Both the Anthropic and OpenAI SDKs append their own canonical path
# (`/v1/messages`, `/v1/chat/completions`) to this base, so we deliberately
# do NOT include `/v1` here — that would double-path under the Anthropic
# SDK. Override via `BRAINTRUST_API_URL` for self-hosted deployments.
_DEFAULT_BRAINTRUST_GATEWAY = "https://gateway.braintrust.dev"


def _project_for_prompt(prompt: FilePrompt, default: str) -> str:
    """Derive the Braintrust project name from the prompt's source path.

    Convention: every direct-LLM prompt lives under
    `apps/backend/app/domain/<module>/llm/prompts/<name>.prompt.md`.
    The third path segment after `domain/` is the owning module — that's
    what we use as the project name so Braintrust's Logs tab segregates
    calls by which module made them (`reviewer`, future `memory`, etc.).
    Projects are auto-created on first request, so adding a new caller
    needs no Braintrust-side setup.

    Falls back to `default` when the path doesn't match the convention
    (e.g., programmatic test prompts with no `source_path`).
    """
    if prompt.source_path is None:
        return default
    parts = prompt.source_path.parts
    try:
        return parts[parts.index("domain") + 1]
    except (ValueError, IndexError):
        return default


class PromptRunnable[OutputT: BaseModel]:
    """Single-shot, structured LLM call. Stateless after construction."""

    def __init__(self, prompt: FilePrompt, output_schema: type[OutputT]) -> None:
        self._prompt = prompt
        self._schema = output_schema

    def _build_model(self) -> BaseChatModel:
        # No `user`/`metadata.user_id` passthrough — langchain's
        # `init_chat_model` forwards `user=` to the Anthropic SDK which
        # rejects it (`TypeError: AsyncMessages.create() got an unexpected
        # keyword argument 'user'`). Prompt-name grouping for observability
        # is already covered by the cache key + structured logs.
        kwargs: dict[str, Any] = dict(self._prompt.model_params)
        settings = get_settings()
        if settings.braintrust_api_key:
            # Route through the Braintrust gateway. base_url + api_key are
            # whitelisted out of `LLMTestCache`'s key derivation (see
            # `llm_test_cache.py:_extract_llm_semantic_fields`) so flipping
            # the gateway on/off does not invalidate cached responses.
            kwargs["base_url"] = settings.braintrust_api_url or _DEFAULT_BRAINTRUST_GATEWAY
            kwargs["api_key"] = settings.braintrust_api_key
            # `x-bt-parent: project_name:<proj>` tells the gateway to log every
            # request to the named Braintrust project. Without it the gateway
            # is a pure pass-through and nothing shows up in the Logs tab.
            # `default_headers` flows through `init_chat_model` to the
            # underlying Anthropic/OpenAI SDK client, which attaches it to
            # every HTTP request. Project name is derived per-prompt from the
            # owning domain module — see `_project_for_prompt`.
            project = _project_for_prompt(self._prompt, settings.braintrust_project)
            kwargs["default_headers"] = {"x-bt-parent": f"project_name:{project}"}
        return init_chat_model(model=self._prompt.model, **kwargs).with_structured_output(
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
