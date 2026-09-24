"""An engine that hit a usage limit pauses until its reset: $SCHUB_ROOT/bench/engine-cooldown.json.

The wording is matched broadly on purpose (the VCC2026 lesson): a missed limit costs
a wasted slice, a false positive only moves the turn to the other engine. Only a time
attached to reset wording counts; without one the pause lasts DEFAULT_HOURS.
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
CLOCK = re.compile(r"\b(?:resets?|try again)\s+(?:at\s+)?(\d{1,2})(?::(\d\d))?\s*(am|pm)?\b(?:\s*\(([^()\n]+)\))?", re.I)


def is_limit(text: str) -> bool:
    return bool(LIMIT.search(text or ""))


def parse_reset(text: str, now: datetime) -> datetime | None:
    """The reset time an engine names ('resets at 2026-09-24T14:00Z', 'resets 3pm (Asia/Dubai)'), in UTC."""
    match = ISO.search(text or "")
    if match:
        try:
            value = datetime.fromisoformat(match.group(1).replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return None  # "2026-02-30": not a date
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc) if value > now else None
    match = CLOCK.search(text or "")
    if not match:
        return None
    hour, minute, ampm, zone = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower(), match.group(4)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        tz = ZoneInfo(zone.strip()) if zone else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class Cooldown:
    def __init__(self, path: Path, now=lambda: datetime.now(timezone.utc)) -> None:
        self.path = path
        self.now = now

    def _state(self) -> dict:
        data = read_json(self.path)
        return data if isinstance(data, dict) else {}

    def mark(self, engine: str, text: str) -> datetime:
        now = self.now()
        parsed = parse_reset(text, now)
        until = min(parsed or now + timedelta(hours=DEFAULT_HOURS), now + MAX_PAUSE)
        state = {**self._state(), engine: {"until": until.isoformat(timespec="seconds"), "parsed": parsed is not None,
                                           "marked": now.isoformat(timespec="seconds"), "reason": (text or "")[-300:]}}
        write_json_atomic(self.path, state)
        return until

    def clear(self, engine: str) -> None:
        """The engine answered (a probe): its pause ends before the time the limit named."""
        state = self._state()
        if engine in state:
            write_json_atomic(self.path, {k: v for k, v in state.items() if k != engine})

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
