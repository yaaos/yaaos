"""Unit tests for `classify_reply` — substitutes the chat model via DI.

No network. `PromptRunnable._build_model` returns a `RunnableLambda` that
emits canned `ClassifyReplyOutput` instances; `with_structured_output` would
do the same wrapping in prod.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableLambda

from app.core.llm import PromptRunnable
from app.domain.reviewer.llm import (
    ClassifyReplyInput,
    ClassifyReplyOutput,
    classify_reply,
    classify_reply_runnable,
)
from app.domain.reviewer.llm.classifier import PriorMessage


class _CannedClassifier(PromptRunnable[ClassifyReplyOutput]):
    def __init__(self, output: ClassifyReplyOutput) -> None:
        super().__init__(classify_reply_runnable()._prompt, ClassifyReplyOutput)
        self._output = output

    def _build_model(self):  # type: ignore[no-untyped-def]
        async def _produce(_messages):  # type: ignore[no-untyped-def]
            return self._output

        return RunnableLambda(_produce)


def _input(reply: str) -> ClassifyReplyInput:
    return ClassifyReplyInput(
        finding_title="x could be None",
        finding_body="caller may pass None",
        rule_id="correctness/null-deref",
        anchor_file="src/foo.py",
        anchor_lines="12",
        code_snippet="def foo(x):\n    return x.bar()",
        reply=reply,
        prior_messages=[],
    )


async def test_classify_reply_returns_acknowledgment() -> None:
    canned = ClassifyReplyOutput(
        intent="acknowledgment",
        confidence=0.92,
        suggested_ack_kind="intentional",
        parsed_claims=None,
    )

    out = await classify_reply(
        _input("By design — None means anonymous."), runnable=_CannedClassifier(canned)
    )

    assert out.intent == "acknowledgment"
    assert out.suggested_ack_kind == "intentional"
    assert out.confidence >= 0.85


async def test_classify_reply_returns_verify_fix() -> None:
    canned = ClassifyReplyOutput(intent="verify_fix", confidence=0.95)

    out = await classify_reply(_input("Fixed in abc123."), runnable=_CannedClassifier(canned))

    assert out.intent == "verify_fix"


async def test_classify_reply_returns_other_for_question() -> None:
    canned = ClassifyReplyOutput(intent="other", confidence=0.7)

    out = await classify_reply(
        _input("How do you suggest I refactor this?"), runnable=_CannedClassifier(canned)
    )

    assert out.intent == "other"


async def test_classify_reply_with_prior_thread() -> None:
    canned = ClassifyReplyOutput(intent="acknowledgment", confidence=0.88, suggested_ack_kind="wontfix")

    inp = ClassifyReplyInput(
        finding_title="t",
        finding_body="b",
        rule_id="r",
        anchor_file="f.py",
        anchor_lines="1",
        code_snippet="x",
        reply="agreed but we won't change it for v1",
        prior_messages=[
            PriorMessage(author_kind="yaaos", body="Original finding"),
            PriorMessage(author_kind="human", body="why?"),
        ],
    )

    out = await classify_reply(inp, runnable=_CannedClassifier(canned))

    assert out.intent == "acknowledgment"
    assert out.suggested_ack_kind == "wontfix"


def test_classify_reply_prompt_loads_without_error() -> None:
    """The prompt file is well-formed and load_prompt succeeds."""
    r = classify_reply_runnable()
    assert r._prompt.name == "classify_reply"
    assert r._prompt.version == 1
    assert r._prompt.tag == "classify_reply.v1"
