#!/usr/bin/env python
"""yaaos worker process — taskiq consumer + outbox drain.

Single-process POC: one event loop runs both the taskiq broker's
consumer loop and the Postgres → Redis drain loop side by side.
See `apps/backend/docs/core_tasks.md` for the architecture.
"""

from __future__ import annotations

import asyncio
import sys


def main() -> int:
    # Side-effect imports: workflow commands + workspace providers + VCS
    # plugins all register at import time. The worker dispatches workflow
    # task bodies, which look up commands/workflows/providers via the
    # registries — those registries are empty until the modules below load.
    # Imported here (outside `core/tasks`) because `core` cannot depend on
    # `plugins` or `testing` under layering rules.
    import app.core.redis as _redis  # noqa: PLC0415

    _redis.bind_pubsub(_redis.RedisPubsub())

    import app.core.agent_gateway as _gw  # noqa: PLC0415

    _gw.bind_subscriber_registry(_gw.SubscriberRegistry())

    from app.domain.orgs.email import _Inbox as _EmailInbox  # noqa: PLC0415
    from app.domain.orgs.email import bind_email_inbox as _bind_inbox  # noqa: PLC0415

    _bind_inbox(_EmailInbox())

    import app.core.coding_agent  # noqa: PLC0415
    import app.core.workflow  # noqa: PLC0415
    import app.domain.reviewer  # noqa: PLC0415

    # Workspace providers registration.
    from app.core.workspace import register_workspace_providers  # noqa: PLC0415

    register_workspace_providers()

    # Structural run-sink assertion — `app.core.coding_agent` (imported above)
    # registers the sink at import time. Crash loud here rather than silently
    # dropping agent stdout in `record_agent_event` mid-flow.
    from app.core.agent_gateway import get_run_sink as _get_run_sink  # noqa: PLC0415

    assert _get_run_sink() is not None, "coding-agent run sink must be registered"

    # Side-effect imports: register `@scheduled` tasks with the broker.
    # Each import triggers the module-level `scheduled(...)` decorator, which
    # wires the task body into the taskiq broker registry.
    import app.core.agent_gateway.subscribers  # noqa: PLC0415
    import app.domain.integrations.scheduler  # noqa: PLC0415
    import app.domain.mcp_proxy.service  # noqa: PLC0415
    import app.domain.orgs.invitation_sweeper  # noqa: PLC0415
    import app.domain.reviewer.orphan_sweep  # noqa: PLC0415
    import app.plugins.claude_code  # noqa: PLC0415
    import app.plugins.github  # noqa: F401, PLC0415
    from app.core.config import get_settings  # noqa: PLC0415

    # Settings refuses to boot if this flag is set in production, so this branch
    # is unreachable in prod (and the testing-layer imports below fail loud in a
    # stripped prod wheel regardless).
    if get_settings().yaaos_coding_agent_stub:
        from app.testing.stub_coding_agent import wrap_all_registered_plugins  # noqa: PLC0415
        from app.testing.stub_workspace import wrap_all_registered_workspace_providers  # noqa: PLC0415

        wrap_all_registered_plugins()
        wrap_all_registered_workspace_providers()

    from app.core.tasks.runtime import run  # noqa: PLC0415

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
