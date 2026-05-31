#!/usr/bin/env python
"""yaaos worker process — taskiq consumer + outbox drain.

Single-process POC: one event loop runs both the taskiq broker's
consumer loop and the Postgres → Redis drain loop side by side.
See `apps/backend/docs/core_tasks.md` for the architecture.
"""

from __future__ import annotations

import asyncio
import os
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

    import app.core.workflow  # noqa: PLC0415
    import app.domain.reviewer  # noqa: PLC0415

    # Startup assertions — crash loud at boot if wiring is wrong rather
    # than surfacing mid-flow. Must run after domain/reviewer import so
    # the workflow-context provider is already installed.
    from app.core.workspace import (  # noqa: PLC0415
        assert_workflow_context_provider,
        register_workspace_recovery_policies,
    )

    register_workspace_recovery_policies()
    assert_workflow_context_provider()

    import app.plugins.claude_code  # noqa: PLC0415
    import app.plugins.github  # noqa: F401, PLC0415

    if os.environ.get("YAAOS_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
        from app.testing.stub_coding_agent import wrap_all_registered_plugins  # noqa: PLC0415
        from app.testing.stub_workspace import wrap_all_registered_workspace_providers  # noqa: PLC0415

        wrap_all_registered_plugins()
        wrap_all_registered_workspace_providers()

    from app.core.tasks.runtime import run  # noqa: PLC0415

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
