"""Server time for every record. Agents do not stamp time themselves: in VCC2026
their own sense of time drifted up to 165 minutes ahead (incident A42)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], str]


def stamp() -> str:
    """UTC, millisecond precision; strings of this form sort in time order."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def seconds_between(earlier: str, later: str) -> float:
    return (parse(later) - parse(earlier)).total_seconds()
