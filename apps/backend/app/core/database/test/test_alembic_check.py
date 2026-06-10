"""Drift check: model __table_args__ must match the live DB schema.

Catches the failure mode where a developer edits a SQLAlchemy model
(adds a column, an index, a constraint) and forgets to run
`alembic revision --autogenerate -m "..."`. The Alembic check API
runs autogenerate's compare-metadata pass without writing a revision
file and surfaces any diff.

Pairs with the dynamic models.py discovery in apps/backend/alembic/env.py:
discovery prevents "added a module, forgot env.py" drift; this test
prevents "edited a model, forgot a revision" drift.
"""

from __future__ import annotations

import re

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from app.core.database import Base, get_engine


@pytest.mark.asyncio
async def test_models_match_db_schema(_migrated_schema: None) -> None:
    """Assert `Base.metadata` matches the migrated test-DB schema.

    Implementation: open a sync DBAPI connection inside the async engine,
    build an Alembic `MigrationContext`, call `compare_metadata`. Any
    non-empty diff is a drift.
    """
    engine = get_engine()
    async with engine.connect() as async_conn:
        diffs = await async_conn.run_sync(_compare)
    if diffs:
        # Render a readable diff for the failure message.
        rendered = "\n".join(f"  - {d!r}" for d in diffs)
        pytest.fail(
            "Model metadata diverges from the migrated DB schema:\n"
            f"{rendered}\n\n"
            'Run `cd apps/backend && uv run alembic revision --autogenerate -m "<msg>"` '
            "to produce a new revision, review it, and commit."
        )


def _include_name(name: str, type_: str, parent_names: dict) -> bool:  # type: ignore[no-untyped-def]
    """Exclude partition children from the autogenerate diff.

    `coding_agent_activity` is PARTITION BY RANGE (created_at); children
    (e.g. coding_agent_activity_p202624) are created and rolled forward by
    maintain_coding_agent_activity_partitions() — not via Alembic revisions.
    Reporting them as "removed tables" would be a false positive.
    See apps/backend/app/core/database/service.py::maintain_coding_agent_activity_partitions.
    """
    if type_ == "table" and re.fullmatch(r"coding_agent_activity_p\d+", name):
        return False
    return True


def _compare(sync_conn) -> list:  # type: ignore[no-untyped-def]
    """Sync inner: build MigrationContext on the bare sync DBAPI connection,
    run compare_metadata against Base.metadata. Returns the diff list."""
    mc = MigrationContext.configure(sync_conn, opts={"include_name": _include_name})
    return list(compare_metadata(mc, Base.metadata))


def test_alembic_config_loads() -> None:
    """Sanity check: alembic.ini parses and the script directory resolves.

    Doesn't need the DB. Catches gross misconfiguration (missing
    alembic.ini, broken `script_location`) that would otherwise only
    surface at boot or in CLI use.
    """
    cfg = Config("alembic.ini")
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    script = ScriptDirectory.from_config(cfg)
    # Branch heads = 1 means a single linear history (current invariant).
    heads = script.get_heads()
    assert len(heads) >= 1, "no Alembic heads — script directory is misconfigured"
