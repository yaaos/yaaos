"""Workflow harness for multi-module service tests.

Provides `scoped_engine` and `scoped_workflow` context managers that give
service tests full engine isolation without touching `core/workflow` internals.
Both helpers import only `core/workflow`'s production `__all__` API.

Import pattern::

    from app.testing.workflow_harness import scoped_engine, scoped_workflow
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.workflow import (
    Workflow,
    WorkflowEngine,
    WorkflowNotFoundError,
    bind_engine,
    get_engine,
    register_workflow,
    unregister_workflow,
)


@contextmanager
def scoped_engine() -> Iterator[WorkflowEngine]:
    """Context manager: swap in a fresh engine for the duration of the block.

    The prior engine (if any) is restored on exit — even if an exception is
    raised. Tests that need to register custom commands or workflows without
    contaminating the process-singleton engine use this helper.
    """
    fresh = WorkflowEngine()
    prior = bind_engine(fresh)
    try:
        yield fresh
    finally:
        bind_engine(prior)


@contextmanager
def scoped_workflow(wf: Workflow) -> Iterator[Workflow]:
    """Context manager: install *wf* on the process-singleton engine for the
    duration of the block, then restore the prior entry (if any) on exit —
    even if an exception is raised.

    If the same (name, version) pair was already registered, the prior entry is
    saved and re-registered on exit. If it was not registered, the workflow is
    simply unregistered on exit.
    """
    engine = get_engine()
    try:
        prior: Workflow | None = engine.get_workflow(wf.name, version=wf.version)
    except WorkflowNotFoundError:
        prior = None

    if prior is not None:
        unregister_workflow(wf.name, wf.version)
    register_workflow(wf)
    try:
        yield wf
    finally:
        unregister_workflow(wf.name, wf.version)
        if prior is not None:
            register_workflow(prior)


__all__ = [
    "scoped_engine",
    "scoped_workflow",
]
