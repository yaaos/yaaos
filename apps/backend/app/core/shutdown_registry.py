"""Process shutdown registries — web and worker.

Zero-dependency standalone module. Every runtime-state module imports from
here at the end of its `__init__.py` to append its `shutdown()` callable.

Keeping the registries here (not in `core/webserver` or `core/tasks`) avoids
a circular import chain:

    core/webserver/__init__ → app_factory → core/database
    core/database/__init__ → core/webserver  ← cycle

Both `core/webserver` and `core/tasks` re-export the public symbols so
callers that `from app.core.webserver import register_web_shutdown_hook`
continue to work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

ShutdownHook = Callable[[], Awaitable[None]]

# ── Web registry ─────────────────────────────────────────────────────────────

_web_shutdown_hooks: list[ShutdownHook] = []


def register_web_shutdown_hook(hook: ShutdownHook) -> None:
    """Append `hook` to the web-process shutdown registry.

    Called at module import time (after `__all__` is defined) by each
    runtime-state module that must run cleanup when the web process exits.
    """
    _web_shutdown_hooks.append(hook)


def iter_web_shutdown_hooks() -> list[ShutdownHook]:
    """Return a snapshot of all registered web-process shutdown hooks.

    Callers iterate the snapshot; the lifespan teardown reverses it so
    the most-recently-registered (most-dependent) modules shut down first.
    """
    return list(_web_shutdown_hooks)


# ── Worker registry ───────────────────────────────────────────────────────────

_worker_shutdown_hooks: list[ShutdownHook] = []


def register_worker_shutdown_hook(hook: ShutdownHook) -> None:
    """Append `hook` to the worker-process shutdown registry.

    Called at module import time by each runtime-state module that must
    run cleanup when the worker process exits.
    """
    _worker_shutdown_hooks.append(hook)


def iter_worker_shutdown_hooks() -> list[ShutdownHook]:
    """Return a snapshot of all registered worker-process shutdown hooks.

    The worker runtime iterates the snapshot in reverse order so the
    most-recently-registered (most-dependent) modules shut down first.
    """
    return list(_worker_shutdown_hooks)
