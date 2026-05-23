"""testing/fake_coding_agent — standalone fake CodingAgentPlugin for tests
that need a registered plugin without wrapping a real one.

`stub_coding_agent` wraps an already-registered real plugin (used when
`YAAOS_CODING_AGENT_STUB=1` in the e2e stack). `fake_coding_agent` is the
opposite: a self-contained `CodingAgentPlugin` impl that tests register
on the fly under any `plugin_id`. Used by service tests that drive a
workflow through the reviewer Workspace commands without a real plugin
bootstrap.
"""

from app.testing.fake_coding_agent.service import (
    FakeCodingAgentPlugin,
    register_fake_coding_agent,
)

__all__ = ["FakeCodingAgentPlugin", "register_fake_coding_agent"]
