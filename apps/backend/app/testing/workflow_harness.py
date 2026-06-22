"""Workflow harness for multi-module service tests.

Re-exports `set_engine_for_tests` from `core/workflow`.  The context manager
is the canonical test seam for engine isolation.
"""

from app.core.workflow import set_engine_for_tests

__all__ = [
    "set_engine_for_tests",
]
