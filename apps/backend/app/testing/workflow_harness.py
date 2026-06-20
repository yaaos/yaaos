"""Workflow harness for multi-module service tests.

Provides `scoped_engine` and `scoped_workflow` context managers that give
service tests full engine isolation without touching `core/workflow` internals.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import app.core.workflow.service as _wf_svc
from app.core.workflow import (
    Workflow,
    WorkflowEngine,
    WorkflowNotFoundError,
    get_engine,
    register_workflow,
)


@contextmanager
def scoped_engine(engine: WorkflowEngine | None = None) -> Iterator[WorkflowEngine]:
    """Context manager: swap in a fresh (or supplied) engine for the duration of
    the block, restoring the prior singleton on exit — even on exception.

    Tests that need to register custom commands or workflows without
    contaminating the process-singleton engine use this helper.
    """
    fresh = engine if engine is not None else WorkflowEngine()
    prior = _wf_svc._engine
    _wf_svc._engine = fresh
    try:
        yield fresh
    finally:
        _wf_svc._engine = prior


@contextmanager
def scoped_workflow(wf: Workflow) -> Iterator[Workflow]:
    """Context manager: install *wf* on the process-singleton engine for the
    duration of the block, then restore the prior entry (if any) on exit —
    even if an exception is raised.

    If the same (name, version) pair was already registered, the prior entry is
    saved and re-registered on exit. If it was not registered, the workflow is
    simply removed on exit.
    """
    engine = get_engine()
    try:
        prior: Workflow | None = engine.get_workflow(wf.name, version=wf.version)
    except WorkflowNotFoundError:
        prior = None

    key = (wf.name, wf.version)
    engine._workflows.pop(key, None)
    engine._recovery_maps.pop(key, None)
    register_workflow(wf)
    try:
        yield wf
    finally:
        engine._workflows.pop(key, None)
        engine._recovery_maps.pop(key, None)
        if prior is not None:
            register_workflow(prior)


__all__ = [
    "scoped_engine",
    "scoped_workflow",
]
