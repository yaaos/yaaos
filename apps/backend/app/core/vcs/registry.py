"""Plugin registry for VCSPlugin instances."""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

from opentelemetry import trace
from pydantic import SecretStr

from app.core.vcs.types import (
    Comment,
    Diff,
    InstallCredentials,
    PluginNotFoundError,
    VCSAuthError,
    VcsInstallNotFound,
    VCSPlugin,
    VCSPullRequest,
)

_tracer = trace.get_tracer(__name__)


class VCSRegistry:
    """VCS plugin map. ContextVar-bound so each test context gets a fresh,
    isolated instance; production rides the import-time default for the process
    lifetime — it never calls bind_vcs_registry(). The ContextVar exists solely
    for per-test isolation (see app/testing/isolation.py)."""

    def __init__(self) -> None:
        self._plugins: dict[str, VCSPlugin] = {}

    def register(self, plugin: VCSPlugin) -> None:
        if plugin.plugin_id in self._plugins:
            raise ValueError(f"VCS plugin {plugin.plugin_id!r} already registered")
        self._plugins[plugin.plugin_id] = plugin

    def replace(self, plugin: VCSPlugin) -> None:
        """Overwrite-or-insert; used by stub helpers."""
        self._plugins[plugin.plugin_id] = plugin

    def get(self, plugin_id: str) -> VCSPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as e:
            raise PluginNotFoundError(plugin_id) from e

    def is_registered(self, plugin_id: str) -> bool:
        return plugin_id in self._plugins

    def ids(self) -> list[str]:
        return list(self._plugins.keys())

    def copy(self) -> VCSRegistry:
        clone = VCSRegistry()
        clone._plugins = dict(self._plugins)
        return clone


_registry_var: ContextVar[VCSRegistry | None] = ContextVar("_vcs_registry_var", default=None)
# Import-time default: plugins that call register_vcs_plugin() at module-import
# time (bootstrap()) land here when no per-test binding is active. Production
# never calls bind_vcs_registry(); the ContextVar exists solely for per-test
# isolation.
_default_registry = VCSRegistry()


def bind_vcs_registry(instance: VCSRegistry) -> None:
    _registry_var.set(instance)


def current_vcs_registry() -> VCSRegistry:
    return _registry_var.get() or _default_registry


def register_vcs_plugin(plugin: VCSPlugin) -> None:
    current_vcs_registry().register(plugin)


def get_plugin(plugin_id: str) -> VCSPlugin:
    return current_vcs_registry().get(plugin_id)


def is_registered(plugin_id: str) -> bool:
    return current_vcs_registry().is_registered(plugin_id)


def registered_plugin_ids() -> list[str]:
    return current_vcs_registry().ids()


async def get_installation_token(plugin_id: str, org_id: UUID) -> str:
    """Top-level dispatcher. Workspace plugins call this for fresh git auth."""
    plugin = get_plugin(plugin_id)
    return await plugin.get_installation_token(org_id)


async def list_installation_repos(plugin_id: str, org_id: UUID) -> list[str]:
    """Top-level dispatcher. Sibling plugins call this to enumerate the org's
    repos without importing the VCS plugin directly."""
    plugin = get_plugin(plugin_id)
    return await plugin.list_installation_repos(org_id)


# ── Typed dispatch helpers — each opens a `vcs.{plugin_id}.{op}` span ───────


async def fetch_pr(plugin_id: str, org_id: UUID, external_id: str) -> VCSPullRequest:
    """Dispatch to `VCSPlugin.fetch_pr` within a `vcs.{plugin_id}.fetch_pr` span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.fetch_pr"):
        return await plugin.fetch_pr(org_id, external_id)


async def fetch_diff(plugin_id: str, org_id: UUID, external_id: str) -> Diff:
    """Dispatch to `VCSPlugin.fetch_diff` within a `vcs.{plugin_id}.fetch_diff` span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.fetch_diff"):
        return await plugin.fetch_diff(org_id, external_id)


async def list_yaaos_comments(plugin_id: str, org_id: UUID, external_id: str) -> list[Comment]:
    """Dispatch to `VCSPlugin.list_yaaos_comments` within a span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.list_yaaos_comments"):
        return await plugin.list_yaaos_comments(org_id, external_id)


async def is_repo_accessible(plugin_id: str, org_id: UUID, repo_external_id: str) -> bool:
    """Dispatch to `VCSPlugin.is_repo_accessible` within a span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.is_repo_accessible"):
        return await plugin.is_repo_accessible(org_id, repo_external_id)


async def detect_force_push(
    plugin_id: str, org_id: UUID, repo_external_id: str, before_sha: str, after_sha: str
) -> bool:
    """Dispatch to `VCSPlugin.detect_force_push` within a span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.detect_force_push"):
        return await plugin.detect_force_push(org_id, repo_external_id, before_sha, after_sha)


async def list_commit_messages(
    plugin_id: str, org_id: UUID, repo_external_id: str, prev_sha: str, head_sha: str
) -> list[str]:
    """Dispatch to `VCSPlugin.list_commit_messages` within a span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.list_commit_messages"):
        return await plugin.list_commit_messages(org_id, repo_external_id, prev_sha, head_sha)


async def post_finding(
    plugin_id: str,
    org_id: UUID,
    external_id: str,
    *,
    file: str | None,
    line_start: int | None,
    line_end: int | None,
    severity: str,
    category: str,
    confidence: str,
    finding_display_id: int,
    rationale: str,
    rule_violated: str,
    rule_source: str,
    suggested_fix: str | None,
) -> str:
    """Dispatch to `VCSPlugin.post_finding` within a `vcs.{plugin_id}.post_finding` span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.post_finding"):
        return await plugin.post_finding(
            org_id,
            external_id,
            file=file,
            line_start=line_start,
            line_end=line_end,
            severity=severity,
            category=category,
            confidence=confidence,
            finding_display_id=finding_display_id,
            rationale=rationale,
            rule_violated=rule_violated,
            rule_source=rule_source,
            suggested_fix=suggested_fix,
        )


async def post_comment(plugin_id: str, org_id: UUID, external_id: str, *, body: str) -> str:
    """Dispatch to `VCSPlugin.post_comment` within a `vcs.{plugin_id}.post_comment` span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.post_comment"):
        return await plugin.post_comment(org_id, external_id, body=body)


async def post_comment_reply(
    plugin_id: str, org_id: UUID, external_id: str, parent_comment_external_id: str, body: str
) -> str:
    """Dispatch to `VCSPlugin.post_comment_reply` within a span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.post_comment_reply"):
        return await plugin.post_comment_reply(org_id, external_id, parent_comment_external_id, body)


async def mark_comments_outdated(
    plugin_id: str, org_id: UUID, external_id: str, comment_external_ids: list[str]
) -> None:
    """Dispatch to `VCSPlugin.mark_comments_outdated` within a span."""
    plugin = get_plugin(plugin_id)
    with _tracer.start_as_current_span(f"vcs.{plugin_id}.mark_comments_outdated"):
        await plugin.mark_comments_outdated(org_id, external_id, comment_external_ids)


def install_url(plugin_id: str, org_id: UUID) -> str | None:
    """Dispatch to `VCSPlugin.install_url` (synchronous — no network IO)."""
    return get_plugin(plugin_id).install_url(org_id)


def validate_settings(plugin_id: str, settings: dict[str, object]) -> dict[str, object]:
    """Dispatch to `VCSPlugin.validate_settings` (synchronous — no network IO)."""
    return get_plugin(plugin_id).validate_settings(settings)


def clone_url(plugin_id: str, repo_external_id: str) -> str:
    """Dispatch to `VCSPlugin.clone_url` (synchronous — no network IO)."""
    return get_plugin(plugin_id).clone_url(repo_external_id)


async def get_install_credentials(plugin_id: str, org_id: UUID, repo_external_id: str) -> InstallCredentials:
    """Return clone URL and installation token for a repo in one call.

    Raises `VcsInstallNotFound` when no active installation exists for the org
    (e.g. the VCS App was uninstalled or the org has no installation row).
    Raises `PluginNotFoundError` when `plugin_id` is not registered.
    """
    plugin = get_plugin(plugin_id)
    url = plugin.clone_url(repo_external_id)
    try:
        token_str = await plugin.get_installation_token(org_id)
    except VCSAuthError as exc:
        raise VcsInstallNotFound(str(exc)) from exc
    return InstallCredentials(clone_url=url, installation_token=SecretStr(token_str))
