"""Scoped cache providers restore the complete boundary, including on errors."""

from __future__ import annotations

import contextvars

import pytest
from pydantic import BaseModel

from app.core.cache import publish, read_record, set_cache_for_tests, update_record
from app.core.cache.pubsub import _RedisProvider
from app.core.cache.service import _get


class _Record(BaseModel):
    value: str


def test_get_returns_default_without_startup_binding() -> None:
    assert isinstance(contextvars.Context().run(_get), _RedisProvider)


def test_set_cache_for_tests_yields_fresh_instance() -> None:
    outer = _get()
    with set_cache_for_tests() as inner:
        assert isinstance(inner, _RedisProvider)
        assert _get() is inner
        assert inner is not outer
    assert _get() is outer


def test_nested_scopes_restore_after_exception() -> None:
    outer = _get()
    with set_cache_for_tests() as mid:
        with pytest.raises(RuntimeError), set_cache_for_tests(scenario="memory") as inner:
            assert _get() is inner
            assert inner is not mid
            raise RuntimeError("scope exit")
        assert _get() is mid
    assert _get() is outer


def test_two_separate_contexts_isolate_bindings() -> None:
    def scoped_instance() -> object:
        with set_cache_for_tests() as instance:
            assert _get() is instance
            return instance

    a = contextvars.copy_context().run(scoped_instance)
    b = contextvars.copy_context().run(scoped_instance)
    assert a is not b


@pytest.mark.asyncio
async def test_recording_preserves_bound_storage_and_copies_events() -> None:
    with set_cache_for_tests(scenario="memory") as outer:
        await update_record("record", fields=_Record(value="before"))
        with set_cache_for_tests(scenario="recording") as recorder:
            assert await read_record("record") == {"value": "before"}
            await update_record("record", fields=_Record(value="after"))
            event = {"nested": {"value": "original"}}
            assert await publish("events", event) == 0
            event["nested"]["value"] = "changed"
            assert recorder.published == [("events", {"nested": {"value": "original"}})]
        assert _get() is outer
        assert await read_record("record") == {"value": "after"}
