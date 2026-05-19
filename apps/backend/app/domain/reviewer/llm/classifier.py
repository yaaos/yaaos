"""`classify_reply` — the only direct LLM call yaaos makes today (plan §6.4).

Text-only reasoning over the finding body, thread history, the developer's
new message, and a small code snippet at the anchor. Code-touching work goes
through `coding_agent` (verify_fix / stale_check / answer_question), not here.

The intent label itself encodes the action — no separate confidence axis.
LLMs are poorly calibrated on probability outputs but reliable on
categorical labels, so we make the classifier pick one of five intents
that map 1:1 onto a `ReplyAction` (see `apply_classified_reply`):

- `acknowledgment_clear`   → post "Noted — skipping in future"
- `acknowledgment_unclear` → post mid-band confirm request
- `verify_fix`             → spawn coding agent → verify in workspace
- `question`               → spawn coding agent → answer in workspace
- `other`                  → store + stay silent
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.core.llm import PromptRunnable, load_prompt

_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify_reply.prompt.md"

ReplyIntentLiteral = Literal[
    "acknowledgment_clear",
    "acknowledgment_unclear",
    "verify_fix",
    "question",
    "other",
]
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
    """Strict structured output from `classify_reply`.

    The `intent` label is the only routing signal — no confidence field.
    `suggested_ack_kind` is required for `acknowledgment_clear` /
    `acknowledgment_unclear` (drives the ack metadata), otherwise null.
    `parsed_claims` is populated when intent is `verify_fix`.
    """

    intent: ReplyIntentLiteral
    suggested_ack_kind: AckKindLiteral | None = None
    parsed_claims: ParsedClaims | None = None


async def classify_reply(input: ClassifyReplyInput) -> ClassifyReplyOutput:
    """Run the reply classifier. No DI — tests rely on the file-colocated
    LLM cache (`LLMTestCache`, session-scoped via `core.llm.pytest_plugin`)
    to replay deterministic responses from disk.
    """
    r = PromptRunnable(load_prompt(_PROMPT_PATH), ClassifyReplyOutput)
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
