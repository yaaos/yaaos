"""Sampler that drops spans whose name appears in a static deny-list.

SQLAlchemy's auto-instrumentation emits a CLIENT span named `connect` on every
new engine connection. Carries `net.peer.{name,port}` and nothing else — no
statement, no status code, no useful timing. With even a warm pool we get one
per pool top-up; with NullPool we get one per session. Never useful at any
signal level.

Sampler-based drop is the cheap path: the span is short-circuited at
`start_span()` and never enters the BSP buffer. Parent context is unchanged
(we wrap an inner sampler), so non-denied siblings sample normally.

Why a deny-list and not a configurable allow-list: the surface area is tiny
(`connect` is the only name we want gone today), and a static tuple makes the
control surface obvious in code review. If the list grows past ~3 names,
extract a builder.
"""

from __future__ import annotations

from collections.abc import Sequence

from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import (
    Decision,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes

# Names of spans the SDK should drop at start_span() time. Matches exactly
# (no regex, no substring) — keep the list short and obvious.
TRACE_SAMPLER_DENY_NAMES: tuple[str, ...] = ("connect",)


class DenyByNameSampler(Sampler):
    """Wrap an inner Sampler; short-circuit to DROP for span names in `deny`."""

    def __init__(self, inner: Sampler, deny: Sequence[str]) -> None:
        self._inner = inner
        self._deny = frozenset(deny)

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if name in self._deny:
            return SamplingResult(
                decision=Decision.DROP,
                attributes=None,
                trace_state=trace_state,
            )
        return self._inner.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return f"DenyByNameSampler(deny={sorted(self._deny)},inner={self._inner.get_description()})"
