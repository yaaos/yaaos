"""migrate() rejects Postgres engines older than the required minimum.

The version-compare logic is extracted into a pure helper so it can be
exercised without standing up a second database engine. The running test DB
is already on the required minimum, so the happy path is covered by every
other service test that calls migrate().
"""

from __future__ import annotations

import pytest

from app.core.database.service import _assert_min_pg_version


def test_version_string_at_minimum_is_accepted() -> None:
    # PG18.0 — exactly at the floor
    _assert_min_pg_version("180000")


def test_version_string_above_minimum_is_accepted() -> None:
    # PG18 minor bump
    _assert_min_pg_version("180001")


def test_version_string_below_minimum_raises_with_readable_message() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        _assert_min_pg_version("170004")
    msg = str(exc_info.value)
    assert "18" in msg, f"error message must name required major version; got: {msg!r}"
    assert "17" in msg, f"error message must name the version seen; got: {msg!r}"


def test_version_string_pg16_raises() -> None:
    with pytest.raises(RuntimeError):
        _assert_min_pg_version("160008")
