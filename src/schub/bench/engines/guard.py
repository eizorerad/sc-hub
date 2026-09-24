"""bench/guard/{claude,codex}: first on PATH inside a lab-agent job, so every start of
an engine (the agent's own, or anything it launches) passes the owner's policy.

A refused engine exits 69 with the reason; an allowed one gets the policy's model and
effort pinned and replaces this process (exec).

    python -m schub.bench.engines.guard claude -p "..."   # what the wrapper runs
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from .base import find_binary
from .policy import EnginePolicy, PolicyError, load

REFUSED = 69


def policy_path() -> Path:
    from ...config import load_settings

    return load_settings().bench_dir / "engine-policy.json"


def pinned(engine: str, args: Sequence[str], policy: EnginePolicy) -> list[str]:
    """The policy's model and effort replace whatever the caller asked for."""
    args = list(args)
    model, effort = policy.model(engine), policy.effort(engine)
    if engine == "claude":
        flags = {"--model": model, "--effort": effort}
        kept, skip = [], False
        for index, arg in enumerate(args):
            if skip:
                skip = False
                continue
            name = arg.split("=", 1)[0]
            if name in flags and flags[name]:
                skip = "=" not in arg and index + 1 < len(args)
                continue
            kept.append(arg)
        return kept + [part for flag, value in flags.items() if value for part in (flag, value)]
    keys = {"model": model, "model_reasoning_effort": effort}
    kept, index = [], 0
    while index < len(args):
        arg = args[index]
        if arg in ("-m", "--model") and model:
            index += 2
            continue
        if arg == "-c" and index + 1 < len(args) and args[index + 1].split("=", 1)[0].strip() in keys \
                and keys[args[index + 1].split("=", 1)[0].strip()]:
            index += 2
            continue
        kept.append(arg)
        index += 1
    extra = [part for key, value in keys.items() if value for part in ("-c", f'{key}="{value}"')]
    if kept[:1] == ["exec"]:  # -c belongs after the subcommand to apply to exec and exec resume alike
        return [kept[0], *extra, *kept[1:]]
    return [*extra, *kept]


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("claude", "codex"):
        sys.stderr.write("usage: guard claude|codex [args...]\n")
        return 2
    engine, args = argv[0], argv[1:]
    project = os.environ.get("SCHUB_GOAL_PROJECT") or None
    try:
        policy = load(policy_path())
    except PolicyError as exc:
        sys.stderr.write(f"{engine} guard: {exc}\n")
        return REFUSED
    if engine not in policy.allowed(project):
        sys.stderr.write(f"{engine} guard: the owner's engine policy ({policy.mode}) does not allow {engine}"
                         f"{' for ' + project if project else ''}; allowed: {', '.join(policy.allowed(project))}\n")
        return REFUSED
    try:
        binary = find_binary(engine)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{engine} guard: {exc}\n")
        return REFUSED
    os.execv(str(binary), [str(binary), *pinned(engine, args, policy)])
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
