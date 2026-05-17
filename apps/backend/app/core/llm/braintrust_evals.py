"""Thin Braintrust `Eval(...)` wrapper used by domain modules.

Owner modules call `create_eval(...)` from their `<module>/eval/*.eval.py`
files. The wrapper:

- Pulls the dataset from Braintrust by `(project=module_name, name=dataset_name)`
  — datasets live in the Braintrust UI; nothing locally.
- Wraps the caller's task with a `BraintrustCallbackHandler` so the LangChain
  trace for each eval row (prompt sent, response received, intermediate chain
  steps) attaches as a child span of the row. Without this, the eval row
  shows only `(input, output, scores)` and you'd have to correlate the
  gateway's flat log by timestamp + `user` tag to see the actual model
  exchange.
- Does NOT wire Braintrust-prompt-as-parameter machinery. yaaof prompts are
  file-based (`<module>/llm/prompts/*.prompt.md`); the parameter UI for
  A/B-testing prompts in the Braintrust experiment view is not used here.

Evals run locally; the experiment is logged to Braintrust via the standard
`BRAINTRUST_API_KEY` env path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from braintrust.framework import EvalHooks, EvalScorer, EvalTask


def _wrap_task(task: Callable[[Any, EvalHooks], Any]) -> Callable[[Any, EvalHooks], Any]:
    """Attach a `BraintrustCallbackHandler` rooted at the current row's span.

    Each eval row gets its own `hooks.span`; installing the handler globally
    inside the wrapped task sets that span as the trace target for every
    LangChain call the task makes. The handler is set per-invocation rather
    than once at construction time so concurrent eval rows route their
    traces to the right place.
    """
    from braintrust_langchain import BraintrustCallbackHandler, set_global_handler  # noqa: PLC0415

    def wrapped(input: Any, hooks: EvalHooks) -> Any:
        handler = BraintrustCallbackHandler(logger=hooks.span)
        set_global_handler(handler)
        return task(input, hooks)

    return wrapped


def create_eval(
    *,
    experiment_name: str,
    module_name: str,
    task: EvalTask,
    scores: Sequence[EvalScorer],
    dataset_name: str,
    max_concurrency: int | None = None,
) -> Any:
    """Construct a `braintrust.Eval` bound to `module_name` as the project.

    Args:
        experiment_name: Shown in the Braintrust UI for this experiment run.
        module_name: Braintrust project (use the owning domain module name).
        task: `(input, hooks) -> output` function.
        scores: Scorer functions (`autoevals` or hand-written `(output, expected) -> Score`).
        dataset_name: Name of the dataset already registered in the Braintrust
            project. Loaded fresh on each invocation via `init_dataset`.
        max_concurrency: Cap on parallel task executions. `None` = unlimited.
            Set to 1 for tasks that call `asyncio.run()` to avoid event-loop
            deadlocks.
    """
    # Local imports so the deps stay optional for callers that don't run evals.
    from braintrust import Eval, init_dataset  # noqa: PLC0415

    return Eval(
        name=module_name,
        experiment_name=experiment_name,
        data=init_dataset(project=module_name, name=dataset_name),
        task=_wrap_task(task),
        scores=list(scores),
        max_concurrency=max_concurrency,
    )
