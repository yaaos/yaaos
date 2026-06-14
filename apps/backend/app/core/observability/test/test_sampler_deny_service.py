"""Service test — DenyByNameSampler drops spans named `connect` and passes all others.

The sampler is wired into the global TracerProvider by `_configure_otel`. This
test constructs a local TracerProvider with the sampler so the assertion is
independent of whether `configure()` has run in the test session.
"""

from __future__ import annotations

import pytest

pytest_plugins: list[str] = []


@pytest.mark.service
@pytest.mark.asyncio
async def test_sampler_drops_spans_named_connect() -> None:
    """Spans named `connect` are dropped; spans with other names pass through."""
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased  # noqa: PLC0415

    from app.core.observability._samplers import (  # noqa: PLC0415
        TRACE_SAMPLER_DENY_NAMES,
        DenyByNameSampler,
    )

    exporter = InMemorySpanExporter()
    sampler = DenyByNameSampler(inner=ParentBased(ALWAYS_ON), deny=TRACE_SAMPLER_DENY_NAMES)
    provider = TracerProvider(sampler=sampler)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = provider.get_tracer(__name__)

    # Denied by name — no attributes.
    with tracer.start_as_current_span("connect"):
        pass

    # Denied by name — has attributes (proves denial is by name, not attribute absence).
    with tracer.start_as_current_span("connect", attributes={"net.peer.name": "postgres"}):
        pass

    # Negative control — must pass through.
    with tracer.start_as_current_span("query"):
        pass

    names = [s.name for s in exporter.get_finished_spans()]
    assert "connect" not in names, f"connect span leaked into exporter: {names}"
    assert "query" in names, f"query span missing from exporter: {names}"

    provider.shutdown()


@pytest.mark.service
def test_deny_by_name_sampler_description() -> None:
    """get_description includes the deny set and the inner sampler description."""
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased  # noqa: PLC0415

    from app.core.observability._samplers import DenyByNameSampler  # noqa: PLC0415

    sampler = DenyByNameSampler(inner=ParentBased(ALWAYS_ON), deny=("connect",))
    desc = sampler.get_description()
    assert "DenyByNameSampler" in desc
    assert "connect" in desc
