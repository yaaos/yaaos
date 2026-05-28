"""Session-end shutdown aggregator: calls all registered web + worker hooks once."""

from app.core.tasks import iter_worker_shutdown_hooks
from app.core.webserver import iter_web_shutdown_hooks


async def shutdown_runtime() -> None:
    """Run every registered shutdown hook exactly once.

    Deduplicates hooks that are registered with both web and worker
    registries (same function object → runs once). Reverses the
    registration order so earlier-registered hooks tear down last.
    Best-effort: exceptions from individual hooks are swallowed so one
    bad hook never prevents subsequent ones from running.
    """
    seen: set[int] = set()
    hooks = []
    for hook in list(iter_web_shutdown_hooks()) + list(iter_worker_shutdown_hooks()):
        if id(hook) in seen:
            continue
        seen.add(id(hook))
        hooks.append(hook)
    for hook in reversed(hooks):
        try:
            await hook()
        except Exception:
            # Best-effort cleanup at session end.
            pass
