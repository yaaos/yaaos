"""Neutral boundary and private per-loop connection regression tests."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import app.core.cache as cache_api
from app.core.cache.pubsub import _RedisProvider


def test_public_interface_expresses_cache_behaviors() -> None:
    assert set(cache_api.__all__) == {
        "CacheError",
        "CacheValidationError",
        "Membership",
        "allow_hit",
        "claim_once",
        "clear_prefix",
        "count_members",
        "delete_record",
        "is_healthy",
        "list_keys",
        "list_members",
        "prune_members",
        "publish",
        "read_record",
        "remove_member",
        "set_cache_for_tests",
        "shutdown",
        "subscribe",
        "subscriber_count",
        "touch_member",
        "update_record",
    }


def test_facade_has_no_sdk_or_configuration_coupling() -> None:
    root = Path(__file__).parents[1]
    for name in ("__init__.py", "service.py", "types.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert not any(
                item == "redis" or item.startswith(("redis.", "app.core.config")) for item in imports
            )


def test_consumers_do_not_import_provider_sdk_or_legacy_interface() -> None:
    app_root = Path(__file__).parents[3]
    for path in app_root.rglob("*.py"):
        relative = path.relative_to(app_root)
        if "test" in relative.parts or relative.parts[:2] == ("core", "cache"):
            continue
        if relative == Path("core/tasks/broker.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert not any(
                name == "redis" or name.startswith(("redis.", "app.core.redis")) for name in imports
            ), relative


@pytest.mark.asyncio
async def test_get_client_returns_same_client_within_loop() -> None:
    provider = _RedisProvider()
    try:
        assert provider._get_client() is provider._get_client()
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
async def test_health_returns_true_when_reachable(redis_or_skip) -> None:
    with cache_api.set_cache_for_tests():
        try:
            assert await cache_api.is_healthy() is True
        finally:
            await cache_api.shutdown()


def test_different_loops_get_different_clients() -> None:
    provider = _RedisProvider()

    async def grab_client() -> object:
        return provider._get_client()

    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        client_a = loop_a.run_until_complete(grab_client())
        client_b = loop_b.run_until_complete(grab_client())
        assert client_a is not client_b
        loop_a.run_until_complete(provider.shutdown())
    finally:
        loop_a.close()
        loop_b.close()
