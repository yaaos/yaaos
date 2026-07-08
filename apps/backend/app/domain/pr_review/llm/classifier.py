"""`classify_comment` — direct LLM call classifying a free-text PR comment.

Narrowed to the three intents an LLM actually asserts here. `unclear` is
never an LLM-asserted label — it's the system-applied fallback for a
low-confidence classification or a comment with no finding anchor (see
`CLASSIFY_COMMENT` in `service.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.llm import PromptRunnable, load_prompt

_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify_comment.prompt.md"

CommentIntent = Literal["question", "claims_fixed", "dispute"]


@dataclass(frozen=True)
class ClassifyCommentInput:
    """Domain-side input shape; mapped to prompt variables in `classify_comment`."""

    finding_body: str | None
    finding_severity: str | None
    comment_body: str


class ClassifyCommentOutput(BaseModel):
    """Strict structured output from `classify_comment`."""

    intent: CommentIntent
    confidence: int = Field(ge=0, le=100)


async def classify_comment(input: ClassifyCommentInput, *, api_key: str | None) -> ClassifyCommentOutput:
    """Run the comment classifier under the org's BYOK Anthropic key (when
    set — `api_key=None` falls through to the process-wide provider key).

    `YAAOS_PR_COMMENT_CLASSIFIER_STUB=1` (e2e docker stack; forbidden in
    production) swaps in a deterministic, content-aware heuristic so the
    comment loop runs end-to-end without an Anthropic key.
    """
    from app.core.config import get_settings  # noqa: PLC0415

    if get_settings().yaaos_pr_comment_classifier_stub:
        return _stub_classify(input)
    r = PromptRunnable(load_prompt(_PROMPT_PATH), ClassifyCommentOutput, api_key=api_key)
    return await r.ainvoke(
        {
            "finding_body": input.finding_body or "",
            "finding_severity": input.finding_severity or "",
            "comment_body": input.comment_body,
        }
    )


def _stub_classify(input: ClassifyCommentInput) -> ClassifyCommentOutput:
    """Deterministic content-aware stub used in the e2e docker stack.

    Keyword conventions match the e2e specs:
      - `fixed` / `addressed` / `resolved` → high-confidence `claims_fixed`.
      - `disagree` / `not a bug` / `wontfix` / `not valid` → high-confidence
        `dispute`.
      - a question mark or a leading question word → high-confidence
        `question`.
      - anything else → low-confidence `question` (forces the `unclear`
        fallback in `CLASSIFY_COMMENT`).
    """
    body = (input.comment_body or "").lower()
    if any(k in body for k in ("fixed", "addressed", "resolved", "pushed a fix")):
        return ClassifyCommentOutput(intent="claims_fixed", confidence=95)
    if any(
        k in body for k in ("disagree", "not a bug", "wontfix", "won't fix", "not valid", "not a real issue")
    ):
        return ClassifyCommentOutput(intent="dispute", confidence=95)
    if "?" in body or body.startswith(("why", "how", "what", "is this")):
        return ClassifyCommentOutput(intent="question", confidence=95)
    return ClassifyCommentOutput(intent="question", confidence=20)
