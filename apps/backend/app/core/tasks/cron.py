"""Minimal 5-field cron expression matcher (minute granularity).

Standard fields, in order: minute (0-59), hour (0-23), day-of-month (1-31),
month (1-12), day-of-week (0-6 where 0=Sunday).

Each field accepts:
- `*` — every value
- `*/N` — every Nth value starting at the field's min
- `N` — the literal value
- `N,M,…` — explicit list
- `N-M` — inclusive range
- Combinations: `N-M/S`, `N,M-O,*/S`

A datetime matches a cron expression iff every field matches the
datetime's corresponding component. `floor_to_minute(dt)` is the
canonical normalization used as the `fire_time` PK part — all workers
race the same row for one minute slot regardless of when within the
minute they evaluated the tick.

No external dep — sized for `* * * * *`, `0 * * * *`, `0 0 * * *`, and
the like, which is all we register. If we need named-day shorthands or
seconds-granularity, swap in `croniter` (already taskiq-compatible).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week (0=Sunday)
)


@dataclass(frozen=True, slots=True)
class CronExpr:
    """Parsed cron expression. The five tuples hold the set of permitted
    integer values for minute, hour, day-of-month, month, and day-of-week
    respectively. Constructed via `CronExpr.parse(...)`."""

    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]

    @classmethod
    def parse(cls, expr: str) -> CronExpr:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron expression must have 5 fields, got {len(parts)}: {expr!r}")
        fields = tuple(_parse_field(parts[i], *_FIELD_RANGES[i]) for i in range(5))
        return cls(minute=fields[0], hour=fields[1], dom=fields[2], month=fields[3], dow=fields[4])

    def matches(self, dt: datetime) -> bool:
        """True iff dt's (minute, hour, dom, month, dow) all fall in their
        allowed sets. Python's `datetime.weekday()` is Mon=0..Sun=6; cron
        uses Sun=0..Sat=6, so we remap before checking dow."""
        return (
            dt.minute in self.minute
            and dt.hour in self.hour
            and dt.day in self.dom
            and dt.month in self.month
            and _python_weekday_to_cron_dow(dt.weekday()) in self.dow
        )


def floor_to_minute(dt: datetime) -> datetime:
    """Strip seconds + microseconds. The result is the canonical
    `fire_time` value for the slot that contains `dt`."""
    return dt.replace(second=0, microsecond=0)


def next_minute(dt: datetime) -> datetime:
    """One minute after the floor of `dt`. Used by the tick test harness
    to step through slots deterministically."""
    return floor_to_minute(dt) + timedelta(minutes=1)


def _python_weekday_to_cron_dow(py_weekday: int) -> int:
    """Python: Mon=0, Sun=6. Cron: Sun=0, Sat=6."""
    return (py_weekday + 1) % 7


def _parse_field(field: str, lo: int, hi: int) -> frozenset[int]:
    """Parse one cron field (e.g. `*/5`, `0`, `0,15,30,45`, `1-5`) into the
    set of integer values it permits."""
    if not field:
        raise ValueError("empty cron field")
    values: set[int] = set()
    for part in field.split(","):
        values.update(_parse_part(part, lo, hi))
    return frozenset(values)


def _parse_part(part: str, lo: int, hi: int) -> set[int]:
    """Parse one comma-element of a cron field."""
    step = 1
    if "/" in part:
        base, step_str = part.split("/", 1)
        step = int(step_str)
        if step <= 0:
            raise ValueError(f"cron step must be positive: {part!r}")
    else:
        base = part
    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        start_str, end_str = base.split("-", 1)
        start = int(start_str)
        end = int(end_str)
    else:
        # Single literal — step is ignored (the literal IS the only value).
        v = int(base)
        if v < lo or v > hi:
            raise ValueError(f"cron value {v} out of range [{lo},{hi}]")
        return {v}
    if start < lo or end > hi or start > end:
        raise ValueError(f"cron range {start}-{end} out of [{lo},{hi}] or inverted")
    return set(range(start, end + 1, step))
