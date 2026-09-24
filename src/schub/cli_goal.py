"""CLI for the lab agent and the engine policy (owner only; no MCP tool changes the policy).

    schub goal-start <project> --goal goal.md     schub engine-policy show
    schub goal-stop <project>                     schub engine-policy set mixed|codex-only|claude-only
    schub goal-status <project>                       [--primary claude] [--claude-model M] [--reason TEXT]
    schub engine-probe                            schub engine-policy grant <project> claude [codex]
                                                  schub engine-policy revoke <project>
    schub eval-run evals/requests.yaml [--engines claude,codex] [--only id,...]
    schub eval-score evals/requests.yaml [--markdown]
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from typing import Any, Callable

from .bench import goal_agent
from .bench.evals import launch, load_requests, markdown, matrix
from .bench.engines import policy as engine_policy
from .bench.engines.probe import probe, summary
from .cli_projects import read_arg
from .service import Hub

SETTINGS = ("primary", "claude_model", "claude_effort", "codex_model", "codex_effort", "weekly_turns",
            "claude_weekly_ceiling")
KINDS = {"weekly_turns": int, "claude_weekly_ceiling": float}


def add_eval_parsers(sub: Any) -> None:
    run = sub.add_parser("eval-run", help="start each evaluation request as a lab-agent goal, once per engine")
    run.add_argument("requests", help="evals/requests.yaml")
    run.add_argument("--engines", default="claude,codex")
    run.add_argument("--only", default="", help="comma-separated request ids")
    run.add_argument("--slice-minutes", type=int, default=45)
    run.add_argument("--max-turns", type=int, default=None, help="cap each request's turn budget")
    run.add_argument("--gpu-minutes", type=int, default=30, help="GPU-minutes a request may use in total")
    run.add_argument("--download-gb", type=int, default=5, help="GB a request may download in total")
    score = sub.add_parser("eval-score", help="score the latest run of each request x engine")
    score.add_argument("requests")
    score.add_argument("--markdown", action="store_true")


def add_goal_parsers(sub: Any) -> None:
    add_eval_parsers(sub)
    start = sub.add_parser("goal-start", help="give a project a lab agent working on a goal (Slurm slices)")
    start.add_argument("project")
    start.add_argument("--goal", required=True, help="goal.md text or its path (front matter: engine, max_turns, ...)")
    sub.add_parser("goal-stop", help="stop a project's lab agent (the next slice ends the chain)").add_argument("project")
    sub.add_parser("goal-status", help="a lab agent's goal, turns, sessions, slices and events").add_argument("project")
    sub.add_parser("engine-probe", help="ask each allowed engine to reply OK (the watchdog does this)")
    policy = sub.add_parser("engine-policy", help="the owner's engine policy for lab agents")
    action = policy.add_subparsers(dest="policy_action", required=True)
    action.add_parser("show")
    set_ = action.add_parser("set")
    set_.add_argument("mode", choices=engine_policy.MODES)
    set_.add_argument("--reason", default="")
    for name in SETTINGS:
        set_.add_argument(f"--{name.replace('_', '-')}", type=KINDS.get(name, str), default=None)
    grant = action.add_parser("grant")
    grant.add_argument("project")
    grant.add_argument("engines", nargs="+", choices=engine_policy.ENGINES)
    grant.add_argument("--note", default="")
    action.add_parser("revoke").add_argument("project")


def goal_handlers(hub: Hub, args: argparse.Namespace) -> dict[str, Callable[[], Any]]:
    path = hub.settings.bench_dir / "engine-policy.json"

    def policy_action() -> Any:
        if args.policy_action == "set":
            chosen = {k: getattr(args, k) for k in SETTINGS if getattr(args, k) is not None}
            result = engine_policy.set_mode(path, args.mode, reason=args.reason, **chosen)
        elif args.policy_action == "grant":
            result = engine_policy.grant(path, args.project, args.engines, note=args.note)
        elif args.policy_action == "revoke":
            result = engine_policy.revoke(path, args.project)
        else:
            result = engine_policy.load(path)
        return {**dataclasses.asdict(result), "engines": summary(hub.settings)}

    def eval_run() -> Any:
        requests = load_requests(Path(args.requests))
        chosen = [r for r in requests if not args.only or r.id in args.only.split(",")]
        return launch(hub.settings, hub.slurm, chosen, [e for e in args.engines.split(",") if e],
                      slice_minutes=args.slice_minutes, max_turns=args.max_turns, gpu_minutes=args.gpu_minutes,
                      download_gb=args.download_gb)

    def eval_score() -> Any:
        rows = matrix(hub.settings, load_requests(Path(args.requests)))
        return markdown(rows) if args.markdown else rows

    return {
        "eval-run": eval_run,
        "eval-score": eval_score,
        "goal-start": lambda: {"job_id": goal_agent.start(hub.settings, hub.slurm, args.project, read_arg(args.goal))},
        "goal-stop": lambda: goal_agent.stop(hub.settings, args.project),
        "goal-status": lambda: goal_agent.status(hub.settings, hub.slurm, args.project),
        "engine-probe": lambda: probe(hub.settings),
        "engine-policy": policy_action,
    }
