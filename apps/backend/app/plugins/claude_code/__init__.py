"""plugins/claude_code — Claude Code CLI wrapper for domain/coding_agent."""

from app.plugins.claude_code.service import ClaudeCodePlugin, bootstrap, get_plugin, set_api_key

__all__ = ["ClaudeCodePlugin", "bootstrap", "get_plugin", "set_api_key"]

# Register at import time.
bootstrap()

# Register the enumerate_skills_v1 workflow + commands. Must run before
# any web handler that calls engine.start("enumerate_skills_v1", ...).
from app.plugins.claude_code.enumerate_workflow import (  # noqa: E402
    EnumerateSkills,
    PersistSkillManifest,
    build_enumerate_skills_workflow,
)


def _register_enumerate_workflow() -> None:
    from app.core.workflow import WorkflowError, get_engine  # noqa: PLC0415

    engine = get_engine()
    for cmd in (EnumerateSkills(), PersistSkillManifest()):
        try:
            engine.register_command(cmd)
        except WorkflowError:
            pass
    try:
        engine.register_workflow(build_enumerate_skills_workflow())
    except WorkflowError:
        pass


_register_enumerate_workflow()

# Register the WorkflowContextProvider that enriches WorkspaceTicketContext
# with clone_url + installation_token. Overwrites the simpler provider
# registered by domain/reviewer (which doesn't fetch VCS credentials).
from app.plugins.claude_code.workflow_context import bootstrap_workflow_context  # noqa: E402

bootstrap_workflow_context()

# Side-effect import: register HTTP routes (/api/claude_code/api_key, /health, /repos/...).
from app.plugins.claude_code import web  # noqa: E402, F401
