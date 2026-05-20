"""Settings aggregator — cross-cutting views over plugin state.

Two responsibilities:
- `get_onboarding_status()` — asks each registered onboarding contributor
  ("is your prereq satisfied?") via the `register_onboarding_contributor`
  registry. Plugins (github, claude_code) register at bootstrap.
- `list_plugins()` — walks the three plugin registries (vcs, coding_agent,
  workspace) and returns each plugin's `PluginMeta` for the discovery
  endpoint that drives the Settings UI.

Layering note: plugins depend on domain, never the reverse. The onboarding
case is solved by a contributor registry (plugins push in). The plugin-list
case is solved by reading the existing registries directly — plugins are
already in them by bootstrap time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel

from app.core.plugin_meta import PluginMeta
from app.core.workspace.service import _PROVIDERS as _WORKSPACE_PROVIDERS
from app.domain.coding_agent.service import _PLUGINS as _CODING_AGENT_PLUGINS
from app.domain.vcs.registry import _PLUGINS as _VCS_PLUGINS

OnboardingCheck = Callable[[UUID], Awaitable[bool]]

_CONTRIBUTORS: dict[str, OnboardingCheck] = {}


def register_onboarding_contributor(name: str, check: OnboardingCheck) -> None:
    """Register a named readiness check. Plugins call this at boot."""
    _CONTRIBUTORS[name] = check


def _reset_contributors_for_tests() -> None:
    _CONTRIBUTORS.clear()


class OnboardingStatus(BaseModel):
    github_app_installed: bool
    anthropic_key_set: bool

    @property
    def all_ready(self) -> bool:
        return self.github_app_installed and self.anthropic_key_set


async def get_onboarding_status(*, org_id: UUID) -> OnboardingStatus:
    """Ask each registered contributor whether its prereq is satisfied."""
    gh = _CONTRIBUTORS.get("github_app_installed")
    cc = _CONTRIBUTORS.get("anthropic_key_set")
    return OnboardingStatus(
        github_app_installed=bool(gh) and await gh(org_id),
        anthropic_key_set=bool(cc) and await cc(org_id),
    )


def list_plugins() -> list[PluginMeta]:
    """Aggregate every registered plugin's metadata across the three registries.

    Returned order: VCS → coding-agent → workspace. Stable so the UI list reads
    consistently across reloads. The Settings page renders one row per entry +
    pairs each with its `/api/<id>/health` for live status.
    """
    out: list[PluginMeta] = []
    for plugin in _VCS_PLUGINS.values():
        out.append(plugin.meta)
    for plugin in _CODING_AGENT_PLUGINS.values():
        out.append(plugin.meta)
    for provider in _WORKSPACE_PROVIDERS.values():
        out.append(provider.meta)
    return out
