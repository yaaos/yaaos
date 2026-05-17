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

from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from sqlalchemy import text

# Importing every models module guarantees `Base.metadata` is fully populated.
# Per the testing-layer rule, this module is allowed to know about every table.
from app.core.audit_log import models as _audit_models  # noqa: F401
from app.core.config import get_settings
from app.core.database import Base
from app.core.database import session as db_session
from app.core.workspace import models as _workspace_models  # noqa: F401
from app.domain.memory.models import LessonRow
from app.domain.pull_requests import models as _pr_models  # noqa: F401
from app.domain.reviewer import models as _reviewer_models  # noqa: F401
from app.domain.tickets import models as _ticket_models  # noqa: F401
from app.plugins.claude_code.models import ClaudeCodeSettingsRow
from app.plugins.github.models import GitHubAppInstallationRow, GitHubSettingsRow

# The whole codebase pins org_id to this constant in M01. Same value the
# domain modules use as the system-actor org.
M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


async def truncate_all_tables() -> None:
    """Wipe every table known to `Base.metadata` in FK-safe order.

    `RESTART IDENTITY CASCADE` resets any sequence-backed primary keys and
    cascades through FK chains; the explicit reverse-order list is belt-and-
    braces for non-CASCADE engines.
    """
    table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    if not table_names:
        return
    async with db_session() as s:
        await s.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
        await s.commit()


async def reset() -> None:
    """Truncate all tables. Reviewer specialists are defined as shipped
    markdown files in `domain/coding_agent/reviewers/`, not DB rows — no
    structural seeding needed.
    """
    await truncate_all_tables()


async def seed_credentials_and_install(*, org_login: str = "acme") -> None:
    """Populate yaaos with credentials and an active install pointing at the
    fake-github seeded org.

    The Fernet-encrypted blobs use placeholder bytes; fake-github accepts any
    bearer token, so the actual key material is never validated downstream.
    The seeded slug matches fake-github's `/app` response (`yaaos-test`).
    """
    fernet = Fernet(get_settings().yaaos_encryption_key.encode())
    async with db_session() as s:
        s.add(
            GitHubSettingsRow(
                id=uuid4(),
                org_id=M01_ORG_ID,
                app_id="12345",
                slug="yaaos-test",
                encrypted_private_key=fernet.encrypt(b"TEST-FAKE-NOT-FOR-PROD-PEM"),
                encrypted_webhook_secret=fernet.encrypt(b"TEST-FAKE-NOT-FOR-PROD-aaaaaaaaaaaaaaaa"),
            )
        )
        s.add(
            GitHubAppInstallationRow(
                id=uuid4(),
                org_id=M01_ORG_ID,
                install_external_id="fake-install-1",
                account_login=org_login,
                status="active",
            )
        )
        s.add(
            ClaudeCodeSettingsRow(
                id=uuid4(),
                org_id=M01_ORG_ID,
                encrypted_anthropic_api_key=fernet.encrypt(b"TEST-FAKE-NOT-FOR-PROD-ANTHROPIC-KEY"),
            )
        )
        await s.commit()


async def seed_lesson(*, repo_external_id: str, title: str, body: str) -> UUID:
    """Insert a single `LessonRow`. Returns its id. Caller chooses the title
    so duplicate-title detection (if needed) lives in the spec, not here.
    """
    lesson_id = uuid4()
    async with db_session() as s:
        s.add(
            LessonRow(
                id=lesson_id,
                org_id=M01_ORG_ID,
                plugin_id="github",
                repo_external_id=repo_external_id,
                title=title,
                body=body,
            )
        )
        await s.commit()
    return lesson_id


def is_dev_env() -> bool:
    """Gate used by every `/api/testing/*` route. Centralised so the rule
    `dev-only routes` lives in one place, not per-handler.
    """
    return get_settings().yaaos_env == "dev"


__all__ = [
    "M01_ORG_ID",
    "is_dev_env",
    "reset",
    "seed_credentials_and_install",
    "seed_lesson",
    "truncate_all_tables",
]
