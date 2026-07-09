"""`domain/pr_review`'s direct LLM caller (comment classification).

Owns the prompt and output schema. `core/llm` provides the mechanics
(file_prompt, prompt_runnable, gateway).
"""

from app.domain.pr_review.llm.classifier import (
    ClassifyCommentInput,
    ClassifyCommentOutput,
    classify_comment,
)

__all__ = [
    "ClassifyCommentInput",
    "ClassifyCommentOutput",
    "classify_comment",
]
