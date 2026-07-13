"""Protocol surface smoke tests — asserts the exact public interface of `core/coding_agent`.

These are the source-of-truth checks that the module's `__all__`, the
`CodingAgentPlugin` Protocol methods, and the absence of retired names are all
correct. No DB, no subprocess, no env — pure import assertions.
"""

from __future__ import annotations

import inspect

import pytest

EXPECTED_ALL = frozenset(
    [
        # Protocol + types
        "CodingAgentPlugin",
        "CommandBuildContext",
        "Invocation",
        "InvokeCodingAgent",
        "Effort",
        "RunResult",
        "RunStatus",
        "StageOptions",
        "Usage",
        "CodingAgentError",
        "PluginNotFoundError",
        "ActivityEvent",
        "ActivityEventKind",
        "ActivityLog",
        "ACTIVITY_EVENT_KINDS",
        # Skills-bundle VOs
        "AgentSource",
        "BundleFile",
        "SkillSource",
        # Dispatch + query APIs
        "register_plugin",
        "replace_plugin",
        "get_plugin",
        "list_plugins",
        "dispatch_invocation",
        "create_run",
        "finalize_run",
        "get_stage_activity",
        # Bundle builder
        "build_skills_bundle_zip",
        "get_shipped_skill_version",
        # Test isolation seam
        "set_coding_agents_for_tests",
        # API key secrets provider
        "build_api_key_secrets_for_org",
        # Credential error type (raised by plugin.build_command)
        "CredentialUnavailableError",
        # Per-org install state
        "CodingAgentAlreadyInstalledError",
        "CodingAgentInstall",
        "CodingAgentNotInstalledError",
        "install_coding_agent",
        "list_coding_agents",
        "uninstall_coding_agent",
        "update_coding_agent_settings",
    ]
)

# Names that were deleted from the Protocol — importing any of them must fail.
RETIRED_NAMES = [
    "CodingAgentCommand",
    "review",
    "incremental_review",
    "verify_fix",
    "stale_check",
    "answer_question",
    "validate_config",
    "health_check_all",
    "registered_plugin_ids",
    "ReviewResult",
    "ValidationResult",
    "HealthStatus",
    "InvocationStatus",
    "InvocationTelemetry",
    "StaleCheckContext",
    "VerifyFixContext",
    "VerifyFixResult",
    "IncrementalReviewContext",
    "IncrementalReviewResult",
    "AnswerQuestionContext",
    "AnswerQuestionResult",
    "OnActivity",
    "ExecSpec",
    "FindingAnchor",
    "CodingAgentCacheMiss",
    "LessonRef",
]


@pytest.mark.service
def test_all_matches_expected_set() -> None:
    """__all__ must match the expected symbol set exactly — no more, no less."""
    import app.core.coding_agent as mod  # noqa: PLC0415

    assert set(mod.__all__) == EXPECTED_ALL


@pytest.mark.service
def test_protocol_has_expected_methods() -> None:
    """CodingAgentPlugin Protocol must expose exactly compile_invocation, parse_result,
    parse_activity_line, and validate_settings as non-dunder, non-`plugin_id` protocol
    methods. api_key_requirement was removed — secrets are forwarded via forward-all."""
    from app.core.coding_agent import CodingAgentPlugin  # noqa: PLC0415

    # Collect Protocol method names (non-dunder, non-plugin_id members
    # that are functions in the Protocol body — i.e. abstract methods).
    proto_methods = {
        name
        for name, _ in inspect.getmembers(CodingAgentPlugin, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert proto_methods == {
        "compile_invocation",
        "build_command",
        "parse_result",
        "parse_activity_line",
        "validate_settings",
        "stage_options",
        "skill_path",
        "render_skill_bundle",
    }


@pytest.mark.service
def test_plugin_id_annotation_on_protocol() -> None:
    """CodingAgentPlugin must declare `plugin_id` as a class-level annotation."""
    from app.core.coding_agent import CodingAgentPlugin  # noqa: PLC0415

    assert "plugin_id" in CodingAgentPlugin.__annotations__


@pytest.mark.service
@pytest.mark.parametrize("name", RETIRED_NAMES)
def test_retired_names_not_importable(name: str) -> None:
    """Every retired name must raise AttributeError on import from app.core.coding_agent."""
    import app.core.coding_agent as mod  # noqa: PLC0415

    assert not hasattr(mod, name), f"Retired symbol {name!r} is still reachable from app.core.coding_agent"
