"""Tests for `YaaosDimensionsSpanProcessor` and `_YaaosLogDimsFilter`.

Verifies that standard yaaos dimensions (`yaaos.org_id`, `yaaos.user_id`,
`yaaos.actor_kind`, `yaaos.workflow_id`, `yaaos.command_id`) are stamped on
every new span and every log record when the corresponding contextvars are set.

Test isolation: uses a local `TracerProvider` + `InMemorySpanExporter` (not the
global OTel provider) and a fresh stdlib root logger with `_YaaosLogDimsFilter`
attached directly — no side effects on global state.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.audit_log import ActorKind
from app.core.auth import (
    command_id_var,
    org_context,
    user_id_var,
    workflow_execution_id_var,
)
from app.core.observability.service import (
    YaaosDimensionsSpanProcessor,
    _YaaosLogDimsFilter,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def span_fixture():
    """Local TracerProvider with `YaaosDimensionsSpanProcessor` + in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(YaaosDimensionsSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    exporter.clear()


# ── Span processor tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_child_span_under_org_context_carries_org_id_and_actor(
    span_fixture: tuple,
) -> None:
    """A span started inside `org_context` carries `yaaos.org_id` + `yaaos.actor_kind`."""
    provider, exporter = span_fixture
    tracer = provider.get_tracer("test")
    org_id = uuid.uuid4()

    async with org_context(org_id, ActorKind.SYSTEM):
        with tracer.start_as_current_span("child-span"):
            pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("yaaos.org_id") == str(org_id), f"got attrs: {attrs}"
    assert attrs.get("yaaos.actor_kind") == "system", f"got attrs: {attrs}"
    # No user_id in background context.
    assert "yaaos.user_id" not in attrs, f"unexpected user_id in background span: {attrs}"


@pytest.mark.asyncio
async def test_span_with_user_id_var_carries_user_id(
    span_fixture: tuple,
) -> None:
    """A span started when `user_id_var` is set carries `yaaos.user_id`."""
    provider, exporter = span_fixture
    tracer = provider.get_tracer("test")
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with org_context(org_id, ActorKind.SYSTEM):
        token = user_id_var.set(user_id)
        try:
            with tracer.start_as_current_span("request-span"):
                pass
        finally:
            user_id_var.reset(token)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("yaaos.user_id") == str(user_id), f"got attrs: {attrs}"
    assert attrs.get("yaaos.org_id") == str(org_id), f"got attrs: {attrs}"


@pytest.mark.asyncio
async def test_span_during_workflow_dispatch_carries_workflow_and_command_id(
    span_fixture: tuple,
) -> None:
    """A span started when workflow + command vars are set carries both dims."""
    provider, exporter = span_fixture
    tracer = provider.get_tracer("test")
    org_id = uuid.uuid4()
    wf_id = str(uuid.uuid4())
    cmd_id = str(uuid.uuid4())

    async with org_context(org_id, ActorKind.SYSTEM):
        wf_token = workflow_execution_id_var.set(wf_id)
        cmd_token = command_id_var.set(cmd_id)
        try:
            with tracer.start_as_current_span("workflow-span"):
                pass
        finally:
            command_id_var.reset(cmd_token)
            workflow_execution_id_var.reset(wf_token)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("yaaos.workflow_id") == wf_id, f"got attrs: {attrs}"
    assert attrs.get("yaaos.command_id") == cmd_id, f"got attrs: {attrs}"


@pytest.mark.asyncio
async def test_background_span_no_user_id(span_fixture: tuple) -> None:
    """Worker/background spans carry org+actor but no user_id."""
    provider, exporter = span_fixture
    tracer = provider.get_tracer("test")
    org_id = uuid.uuid4()

    async with org_context(org_id, ActorKind.WORKSPACE):
        with tracer.start_as_current_span("worker-span"):
            pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("yaaos.org_id") == str(org_id)
    assert attrs.get("yaaos.actor_kind") == "workspace"
    assert "yaaos.user_id" not in attrs


def test_span_outside_context_no_dims(span_fixture: tuple) -> None:
    """A span started outside any context carries no yaaos dims."""
    provider, exporter = span_fixture
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("bare-span"):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert "yaaos.org_id" not in attrs
    assert "yaaos.user_id" not in attrs
    assert "yaaos.workflow_id" not in attrs
    assert "yaaos.command_id" not in attrs


def test_workflow_id_only_set_when_var_is_set(span_fixture: tuple) -> None:
    """When only workflow_execution_id_var is set (no command), only workflow_id appears."""
    provider, exporter = span_fixture
    tracer = provider.get_tracer("test")
    wf_id = str(uuid.uuid4())

    token = workflow_execution_id_var.set(wf_id)
    try:
        with tracer.start_as_current_span("wf-only-span"):
            pass
    finally:
        workflow_execution_id_var.reset(token)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("yaaos.workflow_id") == wf_id
    assert "yaaos.command_id" not in attrs


# ── Log dims filter tests ─────────────────────────────────────────────────────


def _make_log_record(msg: str = "test") -> logging.LogRecord:
    """Create a plain stdlib LogRecord (not from structlog)."""
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


@pytest.mark.asyncio
async def test_log_record_in_org_context_carries_org_dims() -> None:
    """A LogRecord produced inside `org_context` gets `yaaos_org_id` + actor attrs."""
    org_id = uuid.uuid4()
    filt = _YaaosLogDimsFilter()
    record = _make_log_record()

    async with org_context(org_id, ActorKind.SYSTEM):
        filt.filter(record)

    assert getattr(record, "yaaos_org_id", None) == str(org_id), f"record attrs: {vars(record)}"
    assert getattr(record, "yaaos_actor_kind", None) == "system", f"record attrs: {vars(record)}"
    assert not hasattr(record, "yaaos_user_id")


@pytest.mark.asyncio
async def test_log_record_with_user_id_var_set() -> None:
    """A LogRecord produced when user_id_var is set gets `yaaos_user_id`."""
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    filt = _YaaosLogDimsFilter()
    record = _make_log_record()

    async with org_context(org_id, ActorKind.SYSTEM):
        u_token = user_id_var.set(user_id)
        try:
            filt.filter(record)
        finally:
            user_id_var.reset(u_token)

    assert getattr(record, "yaaos_user_id", None) == str(user_id)


@pytest.mark.asyncio
async def test_log_record_with_workflow_and_command_id_vars() -> None:
    """A LogRecord produced during workflow dispatch scope carries workflow + command dims."""
    org_id = uuid.uuid4()
    wf_id = str(uuid.uuid4())
    cmd_id = str(uuid.uuid4())
    filt = _YaaosLogDimsFilter()
    record = _make_log_record()

    async with org_context(org_id, ActorKind.SYSTEM):
        wf_token = workflow_execution_id_var.set(wf_id)
        cmd_token = command_id_var.set(cmd_id)
        try:
            filt.filter(record)
        finally:
            command_id_var.reset(cmd_token)
            workflow_execution_id_var.reset(wf_token)

    assert getattr(record, "yaaos_workflow_execution_id", None) == wf_id
    assert getattr(record, "yaaos_command_id", None) == cmd_id


def test_log_record_outside_context_has_no_dims() -> None:
    """A LogRecord produced outside any context gets no yaaos dim attrs."""
    filt = _YaaosLogDimsFilter()
    record = _make_log_record()
    filt.filter(record)

    assert not hasattr(record, "yaaos_org_id")
    assert not hasattr(record, "yaaos_user_id")
    assert not hasattr(record, "yaaos_workflow_execution_id")
    assert not hasattr(record, "yaaos_command_id")


def test_log_dims_filter_always_returns_true() -> None:
    """The filter must never suppress records — it only annotates."""
    filt = _YaaosLogDimsFilter()
    record = _make_log_record()
    assert filt.filter(record) is True
