"""An engine that hit a usage limit pauses until its reset: $SCHUB_ROOT/bench/engine-cooldown.json.

The wording is matched broadly on purpose (the VCC2026 lesson): a missed limit costs
a wasted slice, a false positive only moves the turn to the other engine. Only a time
attached to reset wording counts; without one the pause lasts DEFAULT_HOURS.

An engine whose turns fail before doing any work (a lost login, a broken CLI) pauses too,
longer each time in a row (FAILURE_PAUSES), so it does not spend a slice and write an
incident every hour; a probe that gets an answer ends the pause early.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..fsio import read_json, write_json_atomic

DEFAULT_HOURS = 5
MAX_PAUSE = timedelta(days=8)  # a weekly window plus a day; "resets in 2099" must not stop an engine for good
LIMIT = re.compile(
    r"usage limit|rate.?limit|limit reached|limit will reset|\bresets? at\b|"
    r"\b(?:weekly|daily|monthly|session) limit\b|hit your limit\b|out of extra usage|"
    r"quota exceeded|too many requests|\b429\b|insufficient_quota|exceeded your current quota|"
    r"spend(?:ing)? cap|insufficient credits|out of credits|credit balance",
    re.I)
ISO = re.compile(r"\b(?:resets?|try again)\s+(?:(?:at|on)\s+)?(20\d\d-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)?)",
                 re.I)
RELATIVE = re.compile(r"\b(?:resets?|try again)\s+in\s+(?:(\d+)\s*d(?:ays?)?)?[\s,]*(?:(\d+)\s*h(?:(?:ou)?rs?)?)?"
                      r"[\s,]*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", re.I)
CLOCK = re.compile(r"\b(?:resets?|try again)\s+(?:at\s+)?(\d{1,2})(?::(\d\d))?\s*(am|pm)?\b(?:\s*\(([^()\n]+)\))?", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# "resets Sep 14, 10pm (Asia/Dubai)", "try again on Sep 14 2026 at 22:00" (Claude's weekly limit, VCC2026 A164)
CALENDAR = re.compile(r"\b(?:resets?|try again)\s+(?:on\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
                      r"(\d{1,2})(?:,?\s+(20\d\d))?\s*,?\s+(?:at\s+)?(\d{1,2})(?::(\d\d))?\s*(am|pm)?\b"
                      r"(?:\s*\(([^()\n]+)\))?", re.I)
FAILURE_PAUSES = (timedelta(0), timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))


def is_limit(text: str) -> bool:
    return bool(LIMIT.search(text or ""))


def parse_reset(text: str, now: datetime) -> datetime | None:
    """The reset time an engine names ('resets at 2026-09-24T14:00Z', 'resets 3pm (Asia/Dubai)',
    'try again in 4 days 3 hours'), in UTC."""
    relative = RELATIVE.search(text or "")
    if relative and any(relative.groups()):
        days, hours, minutes = (int(g) if g else 0 for g in relative.groups())
        return now + timedelta(days=days, hours=hours, minutes=minutes)
    match = ISO.search(text or "")
    if match:
        try:
            value = datetime.fromisoformat(match.group(1).replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return None  # "2026-02-30": not a date
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc) if value > now else None
    match = CALENDAR.search(text or "")
    if match:
        return _calendar(match, now)
    match = CLOCK.search(text or "")
    if not match:
        return None
    clock = _clock(match.group(1), match.group(2), match.group(3))
    if clock is None:
        return None
    tz = _zone(match.group(4))
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _clock(hour_text: str, minute_text: str | None, ampm_text: str | None) -> tuple[int, int] | None:
    hour, minute, ampm = int(hour_text), int(minute_text or 0), (ampm_text or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    return None if hour > 23 or minute > 59 else (hour, minute)


def _zone(name: str | None) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name.strip()) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _calendar(match: re.Match, now: datetime) -> datetime | None:
    """A named day: this year's or next year's (no year given), and only a time still ahead."""
    month, day, year = MONTHS.index(match.group(1).lower()) + 1, int(match.group(2)), match.group(3)
    clock = _clock(match.group(4), match.group(5), match.group(6))
    if clock is None:
        return None
    local_now = now.astimezone(_zone(match.group(7)))
    for candidate_year in [int(year)] if year else [local_now.year, local_now.year + 1]:
        try:
            candidate = local_now.replace(year=candidate_year, month=month, day=day, hour=clock[0], minute=clock[1],
                                          second=0, microsecond=0)
        except ValueError:
            continue  # "Feb 30"
        if candidate > local_now:
            return candidate.astimezone(timezone.utc)
    return None


def reset_hint(details: dict) -> datetime | None:
    """The reset Claude's own rate_limit_event names for a refused turn (more exact than its message)."""
    info = details.get("rate_limit") if isinstance(details, dict) else None
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    try:
        return datetime.fromtimestamp(float(info["resetsAt"]), timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


class Cooldown:
    def __init__(self, path: Path, now=lambda: datetime.now(timezone.utc)) -> None:
        self.path = path
        self.now = now

    def _state(self) -> dict:
        data = read_json(self.path)
        return data if isinstance(data, dict) else {}

    def mark(self, engine: str, text: str, resets: datetime | None = None) -> datetime:
        """Pause after a usage limit: until `resets` (the engine's own number), the time its message
        names, or DEFAULT_HOURS."""
        now = self.now()
        parsed = resets if resets is not None and resets > now else parse_reset(text, now)
        until = min(parsed or now + timedelta(hours=DEFAULT_HOURS), now + MAX_PAUSE)
        return self._pause(engine, until, "usage limit", text, parsed=parsed is not None)

    def failing(self, engine: str, text: str) -> tuple[int, datetime | None]:
        """One more turn that failed before doing any work (a lost login, a broken CLI): the count in a row,
        and the pause it earns (none for a first failure, then FAILURE_PAUSES)."""
        record = self._state().get(f"{engine}:failures")
        count = (record.get("count", 0) if isinstance(record, dict) else 0) + 1
        now = self.now()
        state = {**self._state(), f"{engine}:failures": {"count": count, "last": now.isoformat(timespec="seconds"),
                                                          "reason": (text or "")[-300:]}}
        write_json_atomic(self.path, state)
        pause = FAILURE_PAUSES[min(count, len(FAILURE_PAUSES)) - 1]
        if not pause:
            return count, None
        return count, self._pause(engine, now + pause, "failing", text)

    def answered(self, engine: str) -> None:
        """A turn did work: failures in a row start again from zero."""
        state = self._state()
        if f"{engine}:failures" in state:
            write_json_atomic(self.path, {k: v for k, v in state.items() if k != f"{engine}:failures"})

    def _pause(self, engine: str, until: datetime, kind: str, text: str, parsed: bool = False) -> datetime:
        now = self.now()
        state = {**self._state(), engine: {"until": until.isoformat(timespec="seconds"), "parsed": parsed,
                                           "kind": kind, "marked": now.isoformat(timespec="seconds"),
                                           "reason": (text or "")[-300:]}}
        write_json_atomic(self.path, state)
        return until

    def clear(self, engine: str) -> None:
        """The engine answered (a probe): its pause ends before the time the limit named."""
        state = self._state()
        gone = (engine, f"{engine}:failures")
        if any(key in state for key in gone):
            write_json_atomic(self.path, {k: v for k, v in state.items() if k not in gone})

    def kind(self, engine: str) -> str:
        """Why the engine is paused: "usage limit" or "failing"."""
        record = self._state().get(engine)
        return str(record.get("kind") or "usage limit") if isinstance(record, dict) else ""

    def until(self, engine: str) -> datetime | None:
        record = self._state().get(engine)
        if not isinstance(record, dict):
            return None
        try:
            until = datetime.fromisoformat(record["until"])
        except (KeyError, TypeError, ValueError):
            return None
        return until if until > self.now() else None

    def ready(self, engine: str) -> bool:
        return self.until(engine) is None

    def summary(self, engines: tuple[str, ...]) -> dict[str, str]:
        return {e: ("ready" if self.ready(e) else f"paused until {self.until(e).isoformat(timespec='minutes')}")
                for e in engines}
