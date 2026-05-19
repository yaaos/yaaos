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
    """Run the reply classifier. Pytest runs use the file-colocated LLM
    cache (`LLMTestCache`, session-scoped via `core.llm.pytest_plugin`)
    to replay deterministic responses from disk. The e2e docker stack
    (`yaaos_env == "test"`) has no LLM key + no on-disk cache; in that
    env the classifier falls back to a deterministic, content-aware
    heuristic stub so reviewer-reply flows can run end-to-end without
    an Anthropic key in CI.
    """
    # Stub mode for the e2e docker stack: no LLM key + no on-disk cache.
    # `YAAOS_REVIEWER_CLASSIFIER_STUB=1` opt-in keeps pytest runs (which
    # use the `LLMTestCache`) on the real PromptRunnable path.
    import os  # noqa: PLC0415

    if os.environ.get("YAAOS_REVIEWER_CLASSIFIER_STUB", "").lower() in {"1", "true", "yes"}:
        return _stub_classify(input)
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


def _stub_classify(input: ClassifyReplyInput) -> ClassifyReplyOutput:
    """Deterministic content-aware stub used in the e2e docker stack.

    Matches the keyword conventions used by the M01 e2e specs:
      - `wontfix` / `won't fix` / `not a real issue` → high-confidence
        `acknowledgment_clear` with `kind=wontfix`.
      - `fixed in <sha>` / `addressed in commit <sha>` → `verify_fix`
        with the sha extracted.
      - anything else → `acknowledgment_unclear`.
    """
    import re  # noqa: PLC0415

    body = (input.reply or "").lower()
    fix_match = re.search(r"(?:fixed|addressed)\s+(?:in\s+)?(?:commit\s+)?([0-9a-f]{7,40})", body)
    if fix_match:
        return ClassifyReplyOutput(
            intent="verify_fix",
            parsed_claims=ParsedClaims(fixed_in_commit_sha=fix_match.group(1)),
        )
    if any(k in body for k in ("wontfix", "won't fix", "won't do", "not a real issue", "ignore this")):
        return ClassifyReplyOutput(intent="acknowledgment_clear", suggested_ack_kind="wontfix")
    if any(k in body for k in ("agree", "good point", "will fix", "ack")):
        return ClassifyReplyOutput(intent="acknowledgment_clear", suggested_ack_kind="agree")
    return ClassifyReplyOutput(intent="acknowledgment_unclear")
