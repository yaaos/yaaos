"""Pure-data helpers behind the `/api/testing` HTTP surface.

These are split out of `web.py` so backend integration tests can call them
directly without going through HTTP. The functions are idempotent where it
makes sense (truncate, ensure-builtin-agents); seeders that insert specific
rows fail if the row already exists, surfacing programmer error instead of
silently no-op'ing.

Imports for every module that owns tables happen at the top of this file so
`Base.metadata.sorted_tables` reflects the full schema regardless of which
HTTP routes have been mounted in the calling process.
"""

from __future__ import annotations

from uuid import UUID

# Trigger-imports: importing each module's __init__ ensures `Base.metadata` is
# fully populated so `truncate_all_tables` sees the complete schema regardless
# of which HTTP routes have been mounted in the calling process. We import any
# public symbol (not a Row) — the side effect of the import is what matters.
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.database import truncate_all_tables

# The whole codebase pins org_id to this constant in . Same value the
# domain modules use as the system-actor org.
DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


async def reset() -> None:
    """Truncate all tables and flush Redis rate-limit state.

    DB truncation covers all domain state. Redis rate-limit keys for the
    agent identity-exchange endpoint are also deleted so a subsequent seed
    (bootstrap_owner) isn't blocked by a prior run's burst from the agent
    container's IP.
    """
    from app.core.agent_gateway import delete_identity_exchange_rate_limits  # noqa: PLC0415

    async with db_session() as s:
        await truncate_all_tables(s)
        await s.commit()
    await delete_identity_exchange_rate_limits()


async def seed_github_install(
    *,
    org_login: str = "acme",
    target_org_slug: str | None = None,
) -> None:
    """Seed an active ``github_app_installations`` row + a Claude Code settings
    row on the chosen org. Pre-populates the post-install state so specs that
    aren't about the install handshake itself can skip it.

    ``org_login`` is the GitHub-side ``account_login`` on the install row.
    ``target_org_slug``, when provided, picks the yaaos-side org row to attach
    the rows to (looked up by slug); otherwise the ``DEFAULT_ORG_ID`` stub
    is used. Specs that also log a user in via ``bootstrap_owner`` pass the
    bootstrapped org's slug here so the install lives on the same org as the
    authenticated user — ``/org/<slug>/tickets`` then surfaces webhook-created
    tickets under the route the user is on.

    The platform GitHub App credentials come from ``yaaos_github_app_*`` env
    vars (set on the test compose); no per-org credential row is needed.

    Deliberate side-effect: this seed path calls public service functions
    (``github.record_app_install``, ``byok.set``,
    ``orgs.install_coding_agent``) so it emits the same audit rows and events
    that production writes would produce.
    """
    from app.core import byok as byok_service  # noqa: PLC0415
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug, install_coding_agent  # noqa: PLC0415
    from app.plugins.github import record_app_install  # noqa: PLC0415

    if target_org_slug is not None:
        org = await get_org_by_slug(target_org_slug)
        if org is None:
            raise ValueError(f"org {target_org_slug!r} not found — seed it first via bootstrap_owner")
        target_org_id = org.id
    else:
        target_org_id = DEFAULT_ORG_ID
    async with db_session() as s:
        await record_app_install(
            s,
            org_id=target_org_id,
            install_external_id="fake-install-1",
            account_login=org_login,
        )
        await byok_service.set(
            target_org_id,
            "anthropic",
            "TEST-FAKE-NOT-FOR-PROD-ANTHROPIC-KEY",
            actor=Actor.system(),
            session=s,
        )
        # also write the OrgCodingAgentRow so the bespoke Coding Agent
        # settings page (claude_code's AgentEditor) renders against the
        # configured defaults instead of an empty-state placeholder.
        await install_coding_agent(
            s,
            org_id=target_org_id,
            plugin_id="claude_code",
            settings={},
            actor=Actor.system(),
        )
        await s.commit()


async def seed_repo_skill(*, org_slug: str, repo_external_id: str, skill_name: str) -> None:
    """Write `skill_name` for a connected repo via the public PUT route.

    Calls ``PUT /api/claude_code/repos/{repo_external_id:path}`` through an
    in-process ASGI transport so the seed exercises the same handler code path
    as the settings UI. Auth is bypassed via ``dependency_overrides`` — the
    org_id is resolved from ``org_slug`` and injected directly, since there is
    no real session in a seed context.

    Requires the org to exist and have a Claude Code install (seeded by
    ``seed_github_install`` first). The written `skill_name` is currently
    unused by the live PR-review path (`CodeReview.dispatch` hardcodes
    `skill="pr_review"`); the row is kept for the settings UI's round-trip.
    """
    import json  # noqa: PLC0415
    from urllib.parse import quote  # noqa: PLC0415

    import httpx  # noqa: PLC0415
    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.routing import APIRoute  # noqa: PLC0415

    from app.core.auth import org_id_var, route_security_resolved  # noqa: PLC0415
    from app.core.webserver import get_specs, mount_specs  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")

    resolved_org_id = org.id

    # Locate the PUT route's auth dependency so we can override it precisely.
    # The `require(Action.CODING_AGENT_WRITE)` closure is created once at module
    # load in web.py; extracting it here keeps the override key in sync if the
    # route's action ever changes.
    _put_route_dep: object = None
    _cc_router = get_specs()["claude_code"].router
    for route in _cc_router.routes:
        if (
            isinstance(route, APIRoute)
            and "PUT" in (route.methods or set())
            and "{repo_external_id" in route.path
        ):
            if route.dependencies:
                _put_route_dep = route.dependencies[0].dependency
            break

    def _seed_auth_override() -> None:
        org_id_var.set(resolved_org_id)
        route_security_resolved.set("org_scoped")

    sub_app = FastAPI()
    mount_specs(sub_app, only={"claude_code"})
    if _put_route_dep is not None:
        sub_app.dependency_overrides[_put_route_dep] = _seed_auth_override

    encoded = quote(repo_external_id, safe="")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sub_app),
        base_url="http://test",
    ) as client:
        resp = await client.put(
            f"/api/claude_code/repos/{encoded}",
            content=json.dumps({"skill_name": skill_name}),
            headers={"content-type": "application/json"},
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"seed_repo_skill: PUT /api/claude_code/repos/{repo_external_id}"
            f" returned {resp.status_code}: {resp.text}"
        )


async def seed_lesson(*, repo_external_id: str, title: str, body: str) -> UUID:
    """Insert a single lesson via the public ``lessons.create`` service.

    Returns the generated lesson id. Caller chooses the title so
    duplicate-title detection (if needed) lives in the spec, not here.

    Deliberate side-effect: ``lessons.create`` emits a ``lesson.created``
    audit row — matching what production writes produce.
    """
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.lessons import create as create_lesson  # noqa: PLC0415

    lesson = await create_lesson(
        repo_external_id,
        title,
        body,
        None,
        actor=Actor.system(),
        org_id=DEFAULT_ORG_ID,
        plugin_id="github",
    )
    return lesson.id


async def seed_broken_integration(*, org_slug: str, provider: str = "linear") -> None:
    """Seed an ``mcp_credentials`` row with ``last_refresh_status="failed"`` so e2e
    specs can exercise the broken-creds banner + Integrations settings page
    against a known org. Encrypts placeholder tokens via ``core/secrets``."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.core.secrets import encrypt  # noqa: PLC0415
    from app.domain.integrations import create_credential  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")
    async with db_session() as s:
        await create_credential(
            s,
            org_id=org.id,
            provider=provider,
            encrypted_access_token=encrypt("stub-access").decode(),
            encrypted_refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["read"],
            allowed_tools=[],
            enabled=True,
            upstream_identity=f"{provider}-bot",
            last_refresh_status="failed",
            last_refresh_failed_at=datetime.now(UTC),
        )
        await s.commit()


def is_dev_env() -> bool:
    """Gate used by every `/api/testing/*` route. Centralised so the rule
    `non-prod-only routes` lives in one place, not per-handler. True for
    `dev` and `test`; prod returns 404 via every gated handler.
    """
    return get_settings().is_non_prod


# ── auth-flow helpers ──────────────────────────────────────────────


async def seed_bootstrap_owner(
    *,
    email: str,
    github_id: str,
    org_slug: str,
    display_name: str = "Owner",
    provider: str = "github",
) -> dict[str, str]:
    """Mint user + verified email + oauth_identity + org + Owner
    membership in a single transaction. Idempotent against the same
    ``(email, external_subject, org_slug)``. The provider defaults to
    ``github``; tests using the ``oauth_test`` stub pass ``provider="test"``
    so the subsequent test-stub login matches by identity.

    Deliberate side-effect: calls ``orgs.create_org`` and
    ``orgs.create_membership`` so this seed path emits ``org.created`` and
    ``membership.created`` audit rows — the same as the production
    admin-onboarding path would produce.
    """
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import (  # noqa: PLC0415
        create_email,
        create_oauth_identity,
        create_user,
    )
    from app.domain.orgs import (  # noqa: PLC0415
        create_membership,
        create_org,
    )

    async with db_session() as s:
        user = await create_user(s, display_name=display_name)
        await create_email(
            s,
            user_id=user.id,
            email=email.lower(),
            is_primary=True,
            verified=True,
        )
        await create_oauth_identity(
            s,
            user_id=user.id,
            provider=provider,
            external_subject=str(github_id),
            verified=True,
        )
        org = await create_org(s, slug=org_slug, display_name=org_slug, actor=Actor.system())
        await create_membership(
            s,
            user_id=user.id,
            org_id=org.id,
            role=Role.OWNER,
            handle=email.split("@", 1)[0][:64].lower(),
            actor=Actor.system(),
        )
        # Non-prod: seed IAM ARN + region from env so test-stack agents can
        # exchange identity against mock-aws without manual UI configuration.
        import os as _os  # noqa: PLC0415

        from app.core.tenancy import update_org_fields  # noqa: PLC0415

        seed_arn = _os.environ.get("YAAOS_DEV_SEED_ARN")
        seed_region = _os.environ.get("YAAOS_DEV_SEED_REGION")
        if seed_arn and seed_region:
            await update_org_fields(s, org.id, registered_iam_arn=seed_arn, aws_region=seed_region)
        await s.commit()
        return {"user_id": str(user.id), "org_id": str(org.id), "org_slug": org_slug}


async def seed_user_with_session(*, email: str, raw_session_token: str) -> str:
    """Bind ``raw_session_token`` to the user identified by ``email``. Creates
    the user + verified primary email if missing. Caller sets the
    ``yaaos_session`` cookie to ``raw_session_token`` and the backend resolves
    the session normally."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.core.identity import create_email, create_session, create_user  # noqa: PLC0415
    from app.core.identity import repository as identity_repo  # noqa: PLC0415

    async with db_session() as s:
        existing = await identity_repo.find_user_by_email(s, email)
        if existing is not None:
            user = existing
        else:
            user = await create_user(s, display_name=email.split("@", 1)[0])
            await create_email(
                s,
                user_id=user.id,
                email=email.lower(),
                is_primary=True,
                verified=True,
            )
        await create_session(
            s,
            token_hash=identity_repo.hash_token(raw_session_token),
            user_id=user.id,
            workspace_id=None,
            csrf_token="e2e-csrf",
            ip=None,
            user_agent="e2e",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await s.commit()
        return str(user.id)


def stage_oauth_test_profile(
    *, external_subject: str, primary_email: str, email_verified: bool, display_name: str
) -> None:
    """Stash the next profile the ``oauth_test`` provider will return."""
    from app.core.identity import ProviderProfile  # noqa: PLC0415

    # `plugins.oauth_test` loads only under APP_MODE=test; this helper is
    # imported by code that runs in dev too, so import lazily.
    from app.plugins.oauth_test import set_next_profile  # noqa: PLC0415

    set_next_profile(
        ProviderProfile(
            external_subject=external_subject,
            primary_email=primary_email,
            email_verified=email_verified,
            display_name=display_name,
        )
    )


async def seed_workspace_agent(*, org_slug: str) -> dict[str, str]:
    """Seed a reachable ``workspace_agents`` row for the given org slug.

    Inserts the row with ``state="reachable"`` and a recent ``last_heartbeat_at``
    so the dashboard's 1-hour retention window includes it immediately. Publishes
    ``agent_liveness_changed`` after commit so the dashboard's pure-SSE path
    invalidates the agents query without a manual reload. Returns the agent's
    ``id`` and ``instance_id``.
    """
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415
    from app.testing.seed import seed_agent  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")
    async with db_session() as s:
        result = await seed_agent(org_id=org.id, session=s)
        publish_general_after_commit(
            s,
            org_id=org.id,
            kind=GeneralEventKind.AGENT_LIVENESS_CHANGED,
            payload={},
        )
        await s.commit()
    return {"id": str(result["id"]), "instance_id": result["instance_id"]}


async def deregister_workspace_agent(*, agent_id: UUID) -> dict[str, str]:
    """Simulate an agent's graceful-shutdown "going away" signal.

    Mirrors ``DELETE /api/v1/agent/identity`` for the agent with the given
    canonical ``id``: marks the row offline + ``last_shutdown_at``, runs
    agent-loss cleanup, and publishes ``agent_liveness_changed`` so the
    dashboard flips the card offline live. Drives the graceful-shutdown
    Playwright spec without a real bearer or running container.
    """
    from app.core.agent_gateway import (  # noqa: PLC0415
        get_agent_info,
        get_report_sink,
        mark_agent_shutdown,
    )
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    async with db_session() as s:
        info = await get_agent_info(agent_id, session=s)
    if info is None:
        raise ValueError(f"workspace agent id={agent_id!r} not found")
    org_id = info["org_id"]

    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=agent_id):
        async with db_session() as s:
            await mark_agent_shutdown(agent_id, session=s)
            await get_report_sink().handle_agent_loss({agent_id}, s)
            publish_general_after_commit(
                s,
                org_id=org_id,
                kind=GeneralEventKind.AGENT_LIVENESS_CHANGED,
                payload={},
            )
            await s.commit()
    return {"id": str(agent_id), "instance_id": info["instance_id"]}


def read_and_clear_email_inbox() -> list[dict[str, str]]:
    """Return + clear the in-memory inbox ``domain.orgs.email.send_plain`` writes
    to in test env."""
    from app.testing.seed import read_email_inbox  # noqa: PLC0415

    inbox = read_email_inbox()
    out = [{"to": m.to, "subject": m.subject, "body": m.body} for m in inbox]
    inbox.clear()
    return out


__all__ = [
    "DEFAULT_ORG_ID",
    "deregister_workspace_agent",
    "is_dev_env",
    "read_and_clear_email_inbox",
    "reset",
    "seed_bootstrap_owner",
    "seed_github_install",
    "seed_lesson",
    "seed_repo_skill",
    "seed_user_with_session",
    "seed_workspace_agent",
    "stage_oauth_test_profile",
]
