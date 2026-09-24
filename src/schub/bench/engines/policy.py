"""Which engine the lab agent may use: $SCHUB_ROOT/bench/engine-policy.json.

Only the owner changes it (`schub engine-policy ...`), never an agent and never a
quota: a usage limit pauses an engine (cooldown.py), it does not change the policy.
A missing file means the default (mixed, Claude Code first); a malformed one is an
error, never permission to call another engine.

    mode         codex-only | claude-only | mixed
    primary      in mixed, the engine tried first (the other takes over when it pauses)
    <engine>_model / <engine>_effort   pinned for every run ("" = the CLI's default)
    weekly_turns at most this many lab-agent turns per 7 days across projects (0 = no cap)
    grants       per project, engines allowed beyond the mode: {"engines": [...], "granted": ..., "note": ...}
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..clock import stamp
from ..fsio import write_json_atomic

MODES = ("codex-only", "claude-only", "mixed")
ENGINES = ("claude", "codex")
SETTING = re.compile(r"[A-Za-z0-9._\[\]:-]{0,80}")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class EnginePolicy:
    mode: str = "mixed"
    primary: str = "claude"
    claude_model: str = ""
    claude_effort: str = ""
    codex_model: str = ""
    codex_effort: str = ""
    weekly_turns: int = 0
    grants: dict = field(default_factory=dict)
    updated: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise PolicyError(f"mode must be one of {', '.join(MODES)}, got {self.mode!r}")
        if self.primary not in ENGINES:
            raise PolicyError(f"primary must be one of {', '.join(ENGINES)}, got {self.primary!r}")
        for name in ("claude_model", "claude_effort", "codex_model", "codex_effort"):
            if not isinstance(getattr(self, name), str) or not SETTING.fullmatch(getattr(self, name)):
                raise PolicyError(f"{name} must be a model or effort name")
        if not isinstance(self.weekly_turns, int) or not 0 <= self.weekly_turns <= 10_000:
            raise PolicyError("weekly_turns must be a whole number from 0 to 10000")
        if not isinstance(self.grants, dict):
            raise PolicyError("grants must map projects to {\"engines\": [...]}")
        for project, grant in self.grants.items():
            engines = grant.get("engines") if isinstance(grant, dict) else None
            if not isinstance(engines, list) or not set(engines) <= set(ENGINES) or not engines:
                raise PolicyError(f"grant for {project!r} must list engines from {', '.join(ENGINES)}")

    def model(self, engine: str) -> str:
        return getattr(self, f"{engine}_model")

    def effort(self, engine: str) -> str:
        return getattr(self, f"{engine}_effort")

    def allowed(self, project: str | None = None) -> tuple[str, ...]:
        """Engines this project may use, in the order to try them."""
        other = next(e for e in ENGINES if e != self.primary)
        base = {"codex-only": ("codex",), "claude-only": ("claude",), "mixed": (self.primary, other)}[self.mode]
        granted = tuple(self.grants.get(project, {}).get("engines", ())) if project else ()
        return base + tuple(e for e in granted if e not in base)


def load(path: Path) -> EnginePolicy:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return EnginePolicy()
    except (OSError, ValueError) as exc:
        raise PolicyError(f"{path} is unreadable ({exc}); no engine runs until the owner fixes it") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{path} must hold a JSON object")
    known = {f.name for f in dataclasses.fields(EnginePolicy)}
    unknown = set(data) - known
    if unknown:
        raise PolicyError(f"{path} has unknown fields {sorted(unknown)}")
    try:
        return EnginePolicy(**data)
    except TypeError as exc:
        raise PolicyError(f"{path}: {exc}") from exc


def save(path: Path, policy: EnginePolicy) -> EnginePolicy:
    policy = dataclasses.replace(policy, updated=stamp())
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, dataclasses.asdict(policy))
    return policy


def set_mode(path: Path, mode: str, reason: str = "", **settings: str | int) -> EnginePolicy:
    """The owner's switch (CLI only). A broken file is replaced from the defaults, so the owner can repair it."""
    try:
        base = load(path)
    except PolicyError:
        base = EnginePolicy()
    return save(path, dataclasses.replace(base, mode=mode, reason=reason, **settings))


def grant(path: Path, project: str, engines: Sequence[str], note: str = "") -> EnginePolicy:
    policy = load(path)
    grants = {**policy.grants, project: {"engines": list(engines), "granted": stamp(), "note": note}}
    return save(path, dataclasses.replace(policy, grants=grants))


def revoke(path: Path, project: str) -> EnginePolicy:
    policy = load(path)
    return save(path, dataclasses.replace(policy, grants={k: v for k, v in policy.grants.items() if k != project}))


def order(policy: EnginePolicy, project: str, requested: str, ready: Callable[[str], bool]) -> list[str]:
    """Engines to try for a turn: what the goal asks for, within the policy, minus paused ones."""
    allowed = policy.allowed(project)
    if requested not in ("auto", *ENGINES):
        raise PolicyError(f"engine must be auto, claude or codex, got {requested!r}")
    if requested != "auto":
        if requested not in allowed:
            raise PolicyError(f"the goal asks for {requested}; the owner's policy ({policy.mode}) allows "
                              f"{', '.join(allowed)} for {project}")
        allowed = (requested,)
    return [engine for engine in allowed if ready(engine)]
