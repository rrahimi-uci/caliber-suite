"""Minimal, dependency-free cron expression support for workflow Start triggers.

Supports standard 5-field cron (``minute hour day-of-month month day-of-week``)
with ``*``, ranges (``a-b``), steps (``*/n``, ``a-b/n``), and lists (``a,b,c``).
Day-of-week is ``0-6`` with Sunday = 0 (``7`` also accepted as Sunday).

We deliberately avoid pulling in ``croniter``/``APScheduler``: the scheduler only
needs a "does this expression fire at minute ``dt``?" predicate, which a small
matcher covers exactly and keeps testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_FIELD_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
_FIELD_COUNT = 5


def _parse_field(field: str, lo: int, hi: int, *, name: str) -> set[int]:
    values: set[int] = set()
    for raw_part in field.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"empty term in {name} field")
        spec, sep, step_str = part.partition("/")
        step = 1
        if sep:
            if not step_str.isdigit() or int(step_str) <= 0:
                raise ValueError(f"invalid step {step_str!r} in {name} field")
            step = int(step_str)
        if spec == "*":
            start, end = lo, hi
        elif "-" in spec:
            a_str, b_str = spec.split("-", 1)
            if not (a_str.isdigit() and b_str.isdigit()):
                raise ValueError(f"invalid range {spec!r} in {name} field")
            start, end = int(a_str), int(b_str)
        elif spec.isdigit():
            start = end = int(spec)
        else:
            raise ValueError(f"invalid term {spec!r} in {name} field")
        if start < lo or end > hi or start > end:
            raise ValueError(f"{name} term {part!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


def _split(expr: str) -> list[str]:
    fields = expr.split()
    if len(fields) != _FIELD_COUNT:
        raise ValueError(
            f"cron expression must have {_FIELD_COUNT} fields (got {len(fields)}): {expr!r}"
        )
    return fields


def validate_cron(expr: str) -> None:
    """Raise :class:`ValueError` if ``expr`` is not a valid 5-field cron."""
    fields = _split(expr)
    for value, (lo, hi), name in zip(fields, _FIELD_BOUNDS, _FIELD_NAMES, strict=True):
        _parse_field(value, lo, hi, name=name)


def cron_matches(expr: str, dt: datetime) -> bool:
    """Whether ``expr`` fires at the minute of ``dt`` (seconds ignored).

    ``dt`` should already be in the schedule's target timezone. Day-of-month and
    day-of-week follow the standard cron rule: when *both* are restricted, the
    expression fires if *either* matches; otherwise the restricted field alone
    governs.
    """
    fields = _split(expr)
    minute = _parse_field(fields[0], 0, 59, name="minute")
    hour = _parse_field(fields[1], 0, 23, name="hour")
    dom = _parse_field(fields[2], 1, 31, name="day-of-month")
    month = _parse_field(fields[3], 1, 12, name="month")
    dow = {d % 7 for d in _parse_field(fields[4], 0, 7, name="day-of-week")}

    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False

    # Python weekday(): Mon=0..Sun=6 → cron dow: Sun=0..Sat=6.
    cron_dow = (dt.weekday() + 1) % 7
    dom_restricted = fields[2].strip() != "*"
    dow_restricted = fields[4].strip() != "*"
    dom_ok = dt.day in dom
    dow_ok = cron_dow in dow

    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def next_fires(
    expr: str,
    after: datetime,
    count: int = 5,
    *,
    limit_minutes: int = 366 * 24 * 60,
) -> list[datetime]:
    """Return up to ``count`` future minutes (tz-naive) at which ``expr`` fires.

    Walks forward minute-by-minute from the minute after ``after`` (which, like
    :func:`cron_matches`, should already be in the schedule's target timezone),
    bounded by ``limit_minutes`` so an expression that can never fire (e.g.
    ``0 0 30 2 *``) returns a possibly-shorter list instead of looping forever.
    """
    validate_cron(expr)
    fires: list[datetime] = []
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(limit_minutes):
        if cron_matches(expr, cursor):
            fires.append(cursor)
            if len(fires) >= count:
                break
        cursor += timedelta(minutes=1)
    return fires
