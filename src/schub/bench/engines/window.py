"""Claude's seven-day usage window, as its last rate_limit_event reported it.

The owner's own work (VCC2026's agents, their chats) shares the subscription with the
lab agents, so the policy's claude_weekly_ceiling makes Claude stand down while the
window is that full, until it resets (the VCC2026 rule, which stops its executors at
0.85). Every Claude turn and probe updates $SCHUB_ROOT/bench/claude-rate-limit.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..clock import stamp
from ..fsio import read_json, write_json_atomic

FILE = "claude-rate-limit.json"


def record(bench_dir: Path, info: dict | None) -> None:
    if isinstance(info, dict):
        write_json_atomic(bench_dir / FILE, {"observed": stamp(), "info": info})


def seven_day(bench_dir: Path) -> tuple[float, datetime] | None:
    """(utilization, reset time) of the last observation, if it had the seven-day window."""
    info = (read_json(bench_dir / FILE) or {}).get("info")
    if not isinstance(info, dict):
        return None
    window = ((info.get("unifiedWindows") or {}).get("seven_day")
              or (info if info.get("rateLimitType") == "seven_day" else None))
    try:
        return float(window["utilization"]), datetime.fromtimestamp(float(window["resetsAt"]), timezone.utc)
    except (TypeError, KeyError, ValueError, OverflowError, OSError):
        return None


def above_ceiling(bench_dir: Path, ceiling: float, now: datetime | None = None) -> tuple[float, datetime] | None:
    """The (utilization, reset) that keeps Claude standing down, or None when it may run."""
    if not ceiling:
        return None
    seen = seven_day(bench_dir)
    now = now or datetime.now(timezone.utc)
    if seen is None or seen[0] < ceiling or seen[1] <= now:
        return None
    return seen
