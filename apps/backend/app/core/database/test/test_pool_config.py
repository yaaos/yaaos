"""Pool kwargs derive from settings: prod+dev → QueuePool sized; test → NullPool."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.pool import NullPool

from app.core.database.service import _engine_kwargs


@dataclass
class _StubSettings:
    app_mode: str
    db_pool_size: int = 10
    db_max_overflow: int = 5

    @property
    def is_test(self) -> bool:
        return self.app_mode == "test"

    @property
    def is_non_prod(self) -> bool:
        return self.app_mode != "production"


def test_prod_uses_queue_pool_with_configured_sizes() -> None:
    kwargs = _engine_kwargs(_StubSettings(app_mode="production", db_pool_size=20, db_max_overflow=8))
    assert kwargs["pool_size"] == 20
    assert kwargs["max_overflow"] == 8
    assert kwargs["pool_pre_ping"] is True
    assert "poolclass" not in kwargs


def test_test_uses_null_pool() -> None:
    kwargs = _engine_kwargs(_StubSettings(app_mode="test"))
    assert kwargs["poolclass"] is NullPool
    assert "pool_size" not in kwargs
    assert "max_overflow" not in kwargs
    assert "pool_pre_ping" not in kwargs


def test_engine_kwargs_dev_uses_queue_pool() -> None:
    s = _StubSettings(app_mode="dev", db_pool_size=12, db_max_overflow=4)
    kwargs = _engine_kwargs(s)
    assert "poolclass" not in kwargs  # default = QueuePool
    assert kwargs["pool_size"] == 12
    assert kwargs["max_overflow"] == 4
    assert kwargs["pool_pre_ping"] is True


def test_prod_uses_default_pool_sizes_when_settings_unchanged() -> None:
    kwargs = _engine_kwargs(_StubSettings(app_mode="production"))
    assert kwargs["pool_size"] == 10
    assert kwargs["max_overflow"] == 5
