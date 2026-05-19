"""Unit tests for `classify_reply`.

No mocks, no DI. Each test calls `classify_reply(input)` directly; the
session-scoped `LLMTestCache` (wired by `core.llm.pytest_plugin`)
replays a deterministic response from the colocated
`.langchain_cache.json` checked into git.

The classifier emits one of five categorical intents that map 1:1 onto
the dispatch in `apply_classified_reply`:
- `acknowledgment_clear` — clear wontfix / intentional
- `acknowledgment_unclear` — hedged ack ("yeah I think this is fine")
- `verify_fix` — claims a fix landed
- `question` — asking yaaos to investigate / explain
- `other` — none of the above

To (re)populate the cache after touching the prompt or model config:

    cd apps/backend
    ANTHROPIC_API_KEY=… uv run pytest \
        app/domain/reviewer/test/test_classify_reply.py \
        --allow-llm-calls

then commit the updated `.langchain_cache.json` next to this file.
Default `pytest` runs are offline and fail loudly on a cache miss.
"""

from __future__ import annotations

from app.domain.reviewer.llm import (
    ClassifyReplyInput,
    classify_reply,
)
from app.domain.reviewer.llm.classifier import PriorMessage


def _input(reply: str, prior_messages: list[PriorMessage] | None = None) -> ClassifyReplyInput:
    return ClassifyReplyInput(
        finding_title="x could be None",
        finding_body="caller may pass None",
        rule_id="correctness/null-deref",
        anchor_file="src/foo.py",
        anchor_lines="12",
        code_snippet="def foo(x):\n    return x.bar()",
        reply=reply,
        prior_messages=prior_messages or [],
    )


async def test_classify_reply_returns_acknowledgment_clear_intentional() -> None:
    """Clear wontfix/intentional phrasing → acknowledgment_clear + intentional."""
    out = await classify_reply(_input("By design — None means anonymous."))

    assert out.intent == "acknowledgment_clear"
    assert out.suggested_ack_kind == "intentional"


async def test_classify_reply_returns_acknowledgment_unclear_for_hedged_reply() -> None:
    """Hedged reply → acknowledgment_unclear (system will ask for confirm)."""
    out = await classify_reply(_input("yeah I think this is fine"))

    assert out.intent == "acknowledgment_unclear"


async def test_classify_reply_returns_verify_fix() -> None:
    """'Fixed in <sha>' → verify_fix with the sha parsed out."""
    out = await classify_reply(_input("Fixed in abc123."))

    assert out.intent == "verify_fix"
    assert out.parsed_claims is not None
    assert out.parsed_claims.fixed_in_commit_sha == "abc123"


async def test_classify_reply_returns_question_for_genuine_question() -> None:
    """A genuine investigation question → `question` (subflow answers it)."""
    out = await classify_reply(_input("How big a problem is this?"))

    assert out.intent == "question"


async def test_classify_reply_returns_other_for_off_topic_disagreement() -> None:
    """Off-topic / disagreement that's neither ack nor a real question → other."""
    out = await classify_reply(_input("I disagree, this isn't a bug."))

    assert out.intent == "other"


async def test_classify_reply_uses_prior_thread_context() -> None:
    """Multi-turn ack: thread context + 'won't change for v1' → acknowledgment_clear/wontfix."""
    out = await classify_reply(
        _input(
            "agreed but we won't change it for v1",
            prior_messages=[
                PriorMessage(author_kind="yaaos", body="Original finding"),
                PriorMessage(author_kind="human", body="why?"),
            ],
        )
    )

    assert out.intent == "acknowledgment_clear"
    assert out.suggested_ack_kind == "wontfix"
