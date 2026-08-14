"""Clock and ID ports plus their trivial implementations.

This is the only module under ``src/dagvane`` permitted to import ``uuid`` or
read wall-clock time (enforced by a contract test). Everything else receives
time and identifiers by injection, which is what makes runs reproducible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from dagvane.domain.models import SpecError


class Clock(Protocol):
    def now_iso(self) -> str:
        """Current instant as ISO-8601 UTC with millisecond precision (``...Z``)."""
        ...


class IdSource(Protocol):
    def new_id(self, kind: str) -> str:
        """A new unique identifier for ``kind`` (``run``, ``event``, ``op``, ``call``)."""
        ...


def format_iso_ms(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso_ms(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SpecError(f"invalid ISO-8601 timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise SpecError(f"timestamp {value!r} must carry a UTC offset")
    return parsed.astimezone(UTC)


class SystemClock:
    def now_iso(self) -> str:
        return format_iso_ms(datetime.now(UTC))


class FixedClock:
    """Deterministic clock: starts at ``start`` and advances ``step_ms`` per reading."""

    def __init__(self, start: str, step_ms: int) -> None:
        self._current = parse_iso_ms(start)
        self._step = timedelta(milliseconds=step_ms)

    def now_iso(self) -> str:
        value = format_iso_ms(self._current)
        self._current = self._current + self._step
        return value


class SystemIds:
    def new_id(self, kind: str) -> str:
        return f"{kind}-{uuid.uuid4().hex}"


class SequentialIds:
    """Deterministic ids: ``{kind}-{seed}-{counter:06d}`` with per-kind counters."""

    def __init__(self, seed: str) -> None:
        self._seed = seed
        self._counters: dict[str, int] = {}

    def new_id(self, kind: str) -> str:
        count = self._counters.get(kind, 0) + 1
        self._counters[kind] = count
        return f"{kind}-{self._seed}-{count:06d}"
