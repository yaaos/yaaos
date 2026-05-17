"""`classify_reply` — the only direct LLM call yaaos makes today (plan §6.4).

Text-only reasoning over the finding body, thread history, the developer's
new message, and a small code snippet at the anchor. Code-touching work goes
through `coding_agent` (verify_fix / stale_check), not here.

The aggregate decides what to do with the classifier output based on the
plan §10.3 confidence rubric:
- ≥ 0.85 → act (transition state, route to handler).
- 0.60-0.84 → treat as a question / post a confirmation reply.
- < 0.60 → store, do nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.llm import PromptRunnable, load_prompt

_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify_reply.prompt.md"

ReplyIntentLiteral = Literal["acknowledgment", "verify_fix", "other"]
AckKindLiteral = Literal["intentional", "wontfix"]


@dataclass(frozen=True)
class PriorMessage:
    author_kind: Literal["yaaos", "human"]
    body: str


@dataclass(frozen=True)
class ClassifyReplyInput:
    """Domain-side input shape; mapped to prompt variables in `classify_reply`."""

    finding_title: str
    finding_body: str
    rule_id: str
    anchor_file: str
    anchor_lines: str  # e.g. "12-15" or "42"
    code_snippet: str
    reply: str
    prior_messages: list[PriorMessage]


class ParsedClaims(BaseModel):
    fixed_in_commit_sha: str | None = None


class ClassifyReplyOutput(BaseModel):
    """Strict structured output from `classify_reply`."""

    intent: ReplyIntentLiteral
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_ack_kind: AckKindLiteral | None = None
    parsed_claims: ParsedClaims | None = None


def classify_reply_runnable() -> PromptRunnable[ClassifyReplyOutput]:
    """Lazy-built runnable. Tests can substitute by subclassing `PromptRunnable`."""
    return PromptRunnable(load_prompt(_PROMPT_PATH), ClassifyReplyOutput)


async def classify_reply(
    input: ClassifyReplyInput,
    *,
    runnable: PromptRunnable[ClassifyReplyOutput] | None = None,
) -> ClassifyReplyOutput:
    """Run the classifier. Caller may inject a substitute runnable for tests."""
    r = runnable or classify_reply_runnable()
    return await r.ainvoke(
        {
            "finding_title": input.finding_title,
            "finding_body": input.finding_body,
            "rule_id": input.rule_id,
            "anchor_file": input.anchor_file,
            "anchor_lines": input.anchor_lines,
            "code_snippet": input.code_snippet,
            "reply": input.reply,
            "prior_messages": [{"author_kind": m.author_kind, "body": m.body} for m in input.prior_messages],
        }
    )
