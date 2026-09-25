"""Can each allowed engine answer right now? "Reply with exactly OK", no tools.

The watchdog runs it every PROBE_EVERY_H while a lab agent has work, so a broken
login or a CLI update shows up in the cluster overview before a slice is wasted:
    claude: ok (2.1.280) · codex: paused until 14:00 (usage limit)
A usage limit found here pauses the engine like one found in a real turn.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...config import Settings
from ..clock import stamp
from ..fsio import read_json, write_json_atomic
from .base import Engine, Turn, credential_fingerprint, install_guards, version
from .claude import Claude
from .codex import Codex
from .cooldown import Cooldown, reset_hint
from . import window
from .policy import PolicyError, load

PROBE_EVERY_H = 6
PROMPT = "Reply with exactly: OK"
ADAPTERS: dict[str, Engine] = {"claude": Claude(), "codex": Codex()}


def probe_path(settings: Settings) -> Path:
    return settings.bench_dir / "engine-probe.json"


def probe(settings: Settings, engines: tuple[str, ...] | None = None, timeout_s: int = 120) -> dict:
    policy = load(settings.bench_dir / "engine-policy.json")
    cooldown = Cooldown(settings.bench_dir / "engine-cooldown.json")
    guards = install_guards(settings.bench_dir)
    results = {}
    for name in engines or tuple(dict.fromkeys(policy.allowed() + tuple(
            e for grant in policy.grants.values() for e in grant["engines"]))):
        adapter = ADAPTERS[name]
        with tempfile.TemporaryDirectory(prefix=f"schub-probe-{name}-") as folder:
            outcome = adapter.run(Turn(prompt=PROMPT, cwd=Path(folder), run_dir=Path(folder) / "run",
                                       timeout_s=timeout_s, model=policy.model(name), effort=policy.effort(name)),
                                  binary=str(guards / name))
        ok = outcome.status == "ok" and outcome.text.strip().strip(".").upper() == "OK"
        if name == "claude":
            window.record(settings.bench_dir, outcome.details.get("rate_limit"))
        if outcome.status == "usage_limited":
            cooldown.mark(name, outcome.error, resets=reset_hint(outcome.details))
        elif ok:
            cooldown.clear(name)  # answering again (a cap raised, a window reset early)
        results[name] = {"ok": ok, "status": outcome.status, "at": stamp(), "detail": (outcome.error or
                         outcome.text)[-200:], "version": version(str(guards / name)) if ok else "",
                         "login": credential_fingerprint(name)}
    write_json_atomic(probe_path(settings), {**(read_json(probe_path(settings)) or {}), **results})
    return results


def due(settings: Settings, now: datetime | None = None) -> bool:
    """An engine the policy allows has not been asked for PROBE_EVERY_H (an engine it no longer allows
    keeps its old record and must not make every watchdog run probe again)."""
    now = now or datetime.now(timezone.utc)
    data = read_json(probe_path(settings)) or {}
    engines = _in_use(settings) or set(data)
    times = []
    for engine in engines:
        try:
            times.append(datetime.fromisoformat(data[engine]["at"]))
        except (KeyError, TypeError, ValueError):
            return True  # never asked
    return not times or now - min(times) >= timedelta(hours=PROBE_EVERY_H)


def _in_use(settings: Settings) -> set[str]:
    """The engines the owner's policy lets a lab agent use (by mode or a project's grant); empty if unreadable."""
    try:
        policy = load(settings.bench_dir / "engine-policy.json")
    except PolicyError:
        return set()
    return set(policy.allowed()) | {e for grant in policy.grants.values() for e in grant["engines"]}


def summary(settings: Settings) -> list[str]:
    """One line per engine the policy uses, for the cluster overview."""
    data = read_json(probe_path(settings)) or {}
    cooldown = Cooldown(settings.bench_dir / "engine-cooldown.json")
    lines = []
    for name in sorted(_in_use(settings) or set(data) | {"claude", "codex"}):
        until = cooldown.until(name)
        record = data.get(name) or {}
        if until is not None:
            why = "not answering; if its sign-in expired, sign in again" if cooldown.kind(name) == "failing" \
                else "usage limit"
            lines.append(f"{name}: paused until {until.strftime('%Y-%m-%d %H:%M')} UTC ({why})")
        elif record:
            state = "ok" if record.get("ok") else f"not answering ({record.get('status')}: {record.get('detail', '')[:80]})"
            lines.append(f"{name}: {state}, checked {record.get('at', '')[:16]}")
    week = window.seven_day(settings.bench_dir)
    if week is not None:
        ceiling = load_safely(settings)
        extra = f" (the lab agent stops at {ceiling:.0%})" if ceiling else ""
        lines.append(f"claude weekly window: {week[0]:.0%} used{extra}, resets {week[1].isoformat(timespec='minutes')}")
    return lines


def load_safely(settings: Settings) -> float:
    try:
        return load(settings.bench_dir / "engine-policy.json").claude_weekly_ceiling
    except PolicyError:
        return 0.0
