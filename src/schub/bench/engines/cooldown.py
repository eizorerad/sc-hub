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
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...locking import LockTimeout, exclusive
from ..fsio import read_json, write_json_atomic

DEFAULT_HOURS = 5
MAX_PAUSE = timedelta(days=8)  # a weekly window plus a day; "resets in 2099" must not stop an engine for good
UNNAMED_MAX = timedelta(hours=48)  # a limit that names no reset (a spend cap), met again and again
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
# "resets Sep 14, 10pm (Asia/Dubai)" (Claude's weekly limit, VCC2026 A164), "try again at Sep 20th, 2026 3:05 PM"
# (Codex), "resets on Sep 14 2026 at 22:00"
CALENDAR = re.compile(r"\b(?:resets?|try again)\s+(?:(?:on|at)\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                      r"[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?\s*,?\s+(?:at\s+)?(\d{1,2})(?::(\d\d))?"
                      r"\s*(am|pm)?\b(?:\s*\(([^()\n]+)\))?", re.I)
FAILURE_PAUSES = (timedelta(0), timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
FAILURE_WINDOW = timedelta(minutes=30)


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
    """A named day, only if still ahead. Without a year: this year's, or next year's when this year's is long
    gone ("Jan 2" read in late December); one just passed is stale, not a reset a year away."""
    month, day, year = MONTHS.index(match.group(1).lower()) + 1, int(match.group(2)), match.group(3)
    clock = _clock(match.group(4), match.group(5), match.group(6))
    if clock is None:
        return None
    local_now = now.astimezone(_zone(match.group(7)))
    try:
        candidate = local_now.replace(year=int(year) if year else local_now.year, month=month, day=day,
                                      hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if not year and local_now - candidate > timedelta(days=180):
            candidate = candidate.replace(year=candidate.year + 1)
    except ValueError:
        return None  # "Feb 30"
    return candidate.astimezone(timezone.utc) if candidate > local_now else None


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
    """The pauses of every engine, shared by all projects' slices and the probe: each change is a
    read-modify-write under a lock file, so two slices finishing at once never undo each other."""

    def __init__(self, path: Path, now=lambda: datetime.now(timezone.utc)) -> None:
        self.path = path
        self.now = now

    def _state(self) -> dict:
        data = read_json(self.path)
        return data if isinstance(data, dict) else {}

    @contextmanager
    def _changing(self) -> Iterator[dict]:
        """The state to change in place; written back when the block ends."""
        with ExitStack() as stack:
            try:
                stack.enter_context(exclusive(self.path.with_name(self.path.name + ".lock"), wait_s=30,
                                              stale_after_s=60))
            except (LockTimeout, OSError):
                pass  # a stuck lock must not stop the lab agent: change it unguarded
            state = self._state()
            before = dict(state)
            yield state
            if state != before:
                write_json_atomic(self.path, state)

    def mark(self, engine: str, text: str, resets: datetime | None = None) -> datetime:
        """Pause after a usage limit: until `resets` (the engine's own number), the time its message
        names, or DEFAULT_HOURS, doubled (up to UNNAMED_MAX) while the same unnamed limit comes back."""
        now = self.now()
        parsed = resets if resets is not None and resets > now else parse_reset(text, now)
        with self._changing() as state:
            previous = state.get(engine) if isinstance(state.get(engine), dict) else {}
            streak = int(previous.get("streak", 1)) + 1 if self._again(previous, now) else 1
            if parsed is not None:
                until = parsed
            else:
                hours = DEFAULT_HOURS * 2 ** (streak - 1)
                until = now + min(timedelta(hours=hours), UNNAMED_MAX)
            until = min(until, now + MAX_PAUSE)
            state[engine] = self._record(until, "usage limit", text, now, parsed=parsed is not None, streak=streak)
        return until

    def _again(self, previous: dict, now: datetime) -> bool:
        """The same kind of limit, met again soon after the last pause ended (not a new episode)."""
        if previous.get("kind", "usage limit") != "usage limit" or not previous.get("until"):
            return False
        try:
            return now - datetime.fromisoformat(previous["until"]) < timedelta(hours=2)
        except (TypeError, ValueError):
            return False

    def streak(self, engine: str) -> int:
        """How many limits in a row this episode has had (1: a new one, worth telling the student)."""
        record = self._state().get(engine)
        return int(record.get("streak", 1)) if isinstance(record, dict) else 0

    def failing(self, engine: str, text: str, login: str = "") -> tuple[int, datetime | None]:
        """One more turn that failed before doing any work (a lost login, a broken CLI): the count in a row,
        and the pause it earns (none for a first failure, then FAILURE_PAUSES). Failures within
        FAILURE_WINDOW of the last one (several projects' slices in one short outage) count once.
        `login` is the engine's credential fingerprint then: a new sign-in ends the pause (relogged)."""
        now = self.now()
        with self._changing() as state:
            record = state.get(f"{engine}:failures") if isinstance(state.get(f"{engine}:failures"), dict) else {}
            count = int(record.get("count", 0))
            try:
                recent = now - datetime.fromisoformat(record["last"]) < FAILURE_WINDOW
            except (TypeError, KeyError, ValueError):
                recent = False
            if recent and count:
                return count, self._until(state.get(engine), now)
            count += 1
            state[f"{engine}:failures"] = {"count": count, "last": now.isoformat(timespec="seconds"),
                                           "reason": (text or "")[-300:], "login": login}
            pause = FAILURE_PAUSES[min(count, len(FAILURE_PAUSES)) - 1]
            if not pause:
                return count, None
            state[engine] = self._record(now + pause, "failing", text, now, login=login)
        return count, now + pause

    def relogged(self, engine: str, login: str) -> bool:
        """The engine is paused as failing, and its login changed since (the student signed in again):
        the pause and the count end now. True if that happened."""
        with self._changing() as state:
            record = state.get(engine)
            if not isinstance(record, dict) or record.get("kind") != "failing" or not login:
                return False
            if record.get("login", "") == login:
                return False
            state.pop(engine, None)
            state.pop(f"{engine}:failures", None)
        return True

    def answered(self, engine: str) -> None:
        """A turn did work: failures in a row start again from zero."""
        with self._changing() as state:
            state.pop(f"{engine}:failures", None)

    def clear(self, engine: str, only_failing: bool = False) -> None:
        """The engine answered (a probe), or the student started the goal again: its pause ends now
        (`only_failing`: a usage limit stays)."""
        with self._changing() as state:
            record = state.get(engine)
            if only_failing and not (isinstance(record, dict) and record.get("kind") == "failing"):
                state.pop(f"{engine}:failures", None)
                return
            state.pop(engine, None)
            state.pop(f"{engine}:failures", None)

    @staticmethod
    def _record(until: datetime, kind: str, text: str, now: datetime, **extra: object) -> dict:
        return {"until": until.isoformat(timespec="seconds"), "kind": kind, "marked": now.isoformat(timespec="seconds"),
                "reason": (text or "")[-300:], **{"parsed": False, **extra}}

    def kind(self, engine: str) -> str:
        """Why the engine is paused: "usage limit" or "failing"."""
        record = self._state().get(engine)
        return str(record.get("kind") or "usage limit") if isinstance(record, dict) else ""

    def _until(self, record: object, now: datetime) -> datetime | None:
        if not isinstance(record, dict):
            return None
        try:
            until = datetime.fromisoformat(record["until"])
        except (KeyError, TypeError, ValueError):
            return None
        return until if until > now else None

    def until(self, engine: str) -> datetime | None:
        return self._until(self._state().get(engine), self.now())

    def ready(self, engine: str) -> bool:
        return self.until(engine) is None

    def summary(self, engines: tuple[str, ...]) -> dict[str, str]:
        return {e: ("ready" if self.ready(e) else f"paused until {self.until(e).isoformat(timespec='minutes')}")
                for e in engines}
