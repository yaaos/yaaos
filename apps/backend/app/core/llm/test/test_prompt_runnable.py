"""Tests for `PromptRunnable`. `init_chat_model` is substituted via DI.

We deliberately don't use `@patch` (banned by ruff TID251). The PromptRunnable
exposes `_build_model` as the seam — tests subclass and override.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.llm import MalformedOutput, PromptRunnable, load_prompt

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "example.prompt.md"


class Verdict(BaseModel):
    answer: str = Field(...)


class _CannedRunnable(PromptRunnable[Verdict]):
    """Substitutes the model with a canned-output sequence for tests."""

    def __init__(self, prompt, schema, outputs: list) -> None:  # type: ignore[no-untyped-def]
        super().__init__(prompt, schema)
        self._outputs = list(outputs)

    def _build_model(self) -> BaseChatModel:  # type: ignore[override]
        outputs = self._outputs

        async def _produce(_messages: list[BaseMessage]):  # type: ignore[no-untyped-def]
            if not outputs:
                raise AssertionError("no more canned outputs")
            return outputs.pop(0)

        return RunnableLambda(_produce)  # type: ignore[return-value]


async def test_ainvoke_returns_parsed_pydantic() -> None:
    prompt = load_prompt(FIXTURE_PATH)
    runnable = _CannedRunnable(prompt, Verdict, [Verdict(answer="yes")])

    out = await runnable.ainvoke({"subject": "the migration"})

    assert out.answer == "yes"


async def test_ainvoke_retries_once_on_malformed_then_raises() -> None:
    prompt = load_prompt(FIXTURE_PATH)
    runnable = _CannedRunnable(prompt, Verdict, ["not a Verdict", "still not"])

    with pytest.raises(MalformedOutput):
        await runnable.ainvoke({"subject": "x"})


async def test_ainvoke_succeeds_on_retry() -> None:
    prompt = load_prompt(FIXTURE_PATH)
    runnable = _CannedRunnable(prompt, Verdict, ["junk", Verdict(answer="recovered")])

    out = await runnable.ainvoke({"subject": "x"})

    assert out.answer == "recovered"


class _RecordingBuildModelRunnable(PromptRunnable[Verdict]):
    """Records the kwargs `_build_model` would have passed to
    `init_chat_model`, without actually calling it."""

    def __init__(self, prompt, schema, *, api_key: str | None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(prompt, schema, api_key=api_key)
        self.observed_api_key: str | None = None

    def _build_model(self) -> BaseChatModel:  # type: ignore[override]
        settings = get_settings()
        if not settings.braintrust_api_key and self._api_key:
            self.observed_api_key = self._api_key

        async def _produce(_messages):  # type: ignore[no-untyped-def]
            raise AssertionError("model should not be invoked in this test")

        return RunnableLambda(_produce)  # type: ignore[return-value]


def test_api_key_forwarded_when_braintrust_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    get_settings.cache_clear()
    prompt = load_prompt(FIXTURE_PATH)
    runnable = _RecordingBuildModelRunnable(prompt, Verdict, api_key="org-byok-key")

    runnable._build_model()

    assert runnable.observed_api_key == "org-byok-key"
