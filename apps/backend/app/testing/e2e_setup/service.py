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

from cryptography.fernet import Fernet

# Importing every Row class ensures `Base.metadata` is fully populated so
# `truncate_all_tables` sees the complete schema regardless of mount order.
from app.core.audit_log import AuditEntryRow as _AuditEntryRow  # noqa: F401
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.database import truncate_all_tables
from app.core.workspace import WorkspaceRow as _WorkspaceRow  # noqa: F401
from app.domain.lessons import LessonRow as _LessonRow  # noqa: F401
from app.domain.pull_requests import PullRequestRow as _PullRequestRow  # noqa: F401
from app.domain.reviewer import ReviewRow as _ReviewRow  # noqa: F401
from app.domain.tickets import TicketRow as _TicketRow  # noqa: F401
from app.plugins.claude_code import ClaudeCodeSettingsRow as _ClaudeCodeSettingsRow  # noqa: F401
from app.plugins.github import GitHubAppInstallationRow as _GitHubAppInstallationRow  # noqa: F401

# The whole codebase pins org_id to this constant in . Same value the
# domain modules use as the system-actor org.
DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


async def reset() -> None:
    """Truncate all tables. Reviewer specialists are defined as shipped
    markdown files in `domain/coding_agent/reviewers/`, not DB rows — no
    structural seeding needed.
    """
    async with db_session() as s:
        await truncate_all_tables(s)
        await s.commit()


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
    authenticated user — ``/orgs/<slug>/tickets`` then surfaces webhook-created
    tickets under the route the user is on.

    The platform GitHub App credentials come from ``yaaos_github_app_*`` env
    vars (set on the test compose); no per-org credential row is needed.

    Deliberate side-effect: this seed path calls public service functions
    (``github.record_app_install``, ``claude_code.set_api_key``,
    ``orgs.install_coding_agent``) so it emits the same audit rows and events
    that production writes would produce.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.domain.orgs import OrgRow, install_coding_agent  # noqa: PLC0415
    from app.plugins.claude_code import set_api_key  # noqa: PLC0415
    from app.plugins.github import record_app_install  # noqa: PLC0415

    fernet = Fernet(get_settings().yaaos_encryption_key.get_secret_value().encode())
    async with db_session() as s:
        if target_org_slug is not None:
            org = (await s.execute(select(OrgRow).where(OrgRow.slug == target_org_slug))).scalar_one_or_none()
            if org is None:
                raise ValueError(f"org {target_org_slug!r} not found — seed it first via bootstrap_owner")
            target_org_id = org.id
        else:
            target_org_id = DEFAULT_ORG_ID
        await record_app_install(
            s,
            org_id=target_org_id,
            install_external_id="fake-install-1",
            account_login=org_login,
        )
        await set_api_key(
            s,
            org_id=target_org_id,
            encrypted_anthropic_api_key=fernet.encrypt(b"TEST-FAKE-NOT-FOR-PROD-ANTHROPIC-KEY"),
        )
        # also write the OrgCodingAgentRow so the bespoke Coding Agent
        # settings page (claude_code's AgentEditor) renders against the
        # configured defaults instead of an empty-state placeholder.
        from app.core.audit_log import Actor  # noqa: PLC0415

        await install_coding_agent(
            s,
            org_id=target_org_id,
            plugin_id="claude_code",
            settings={},
            actor=Actor.system(),
        )
        await s.commit()


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

    from sqlalchemy import select  # noqa: PLC0415

    from app.core.secrets import encrypt  # noqa: PLC0415
    from app.domain.integrations import create_credential  # noqa: PLC0415
    from app.domain.orgs import OrgRow  # noqa: PLC0415

    async with db_session() as s:
        org = (await s.execute(select(OrgRow).where(OrgRow.slug == org_slug))).scalar_one_or_none()
        if org is None:
            raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")
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
    from app.core.identity import (  # noqa: PLC0415
        create_email,
        create_oauth_identity,
        create_user,
    )
    from app.domain.orgs import (  # noqa: PLC0415
        Role,
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

    # `plugins.oauth_test` loads only under YAAOS_ENV=test; this helper is
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


def read_and_clear_email_inbox() -> list[dict[str, str]]:
    """Return + clear the in-memory inbox ``domain.orgs.email.send_plain`` writes
    to in test env."""
    from app.domain.orgs import get_test_inbox  # noqa: PLC0415

    inbox = get_test_inbox()
    out = [{"to": m.to, "subject": m.subject, "body": m.body} for m in inbox]
    inbox.clear()
    return out


__all__ = [
    "DEFAULT_ORG_ID",
    "is_dev_env",
    "read_and_clear_email_inbox",
    "reset",
    "seed_bootstrap_owner",
    "seed_github_install",
    "seed_lesson",
    "seed_user_with_session",
    "stage_oauth_test_profile",
]
