"""CLI for the bench: the same operations as its MCP tools, JSON output."""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from .bench.models import Actor, CheckSpec
from .bench.service import BenchService
from .bench.watchdog import check
from .cli_projects import read_arg
from .service import Hub


def add_bench_parsers(sub: Any) -> None:
    run = sub.add_parser("bench-run", help="run a cell in the project's kernel on the workbench")
    run.add_argument("project")
    run.add_argument("--code", required=True, help="code, or the path of a file with the code")
    run.add_argument("--why", required=True)
    run.add_argument("--expect", required=True)
    run.add_argument("--setup", action="store_true", help="replayed after a kernel restart")
    run.add_argument("--checks", default="[]", help='JSON list, e.g. [{"name": "file", "params": {"path": "work/x"}}]')
    run.add_argument("--wait", type=float, default=None, help="seconds to wait for the result")
    wait = sub.add_parser("bench-wait", help="wait for a cell: <project>#c0007")
    wait.add_argument("ref")
    wait.add_argument("--wait", type=float, default=None)
    sub.add_parser("bench-interrupt", help="interrupt a running cell").add_argument("ref")
    sub.add_parser("bench-status", help="the workbench and your job slots")
    sub.add_parser("bench-stop", help="stop the workbench now (frees its job slot)")
    sub.add_parser("bench-watchdog", help="run one watchdog check (the watchdog job does this)")
    sub.add_parser("ide-setup", help="let VS Code into the workbench job with the public key on stdin")
    sub.add_parser("ide-proxy", help="VS Code's ProxyCommand: sshd inside the workbench job on stdin/stdout")
    journal = sub.add_parser("bench-journal", help="a project's journal entries")
    journal.add_argument("project")
    journal.add_argument("--since", default=None)
    journal.add_argument("--limit", type=int, default=20)


def _checks(raw: str) -> list[CheckSpec]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--checks is not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError('--checks takes a JSON list, e.g. [{"name": "file", "params": {"path": "work/x"}}]')
    return [CheckSpec.model_validate(c) for c in data]


def bench_handlers(hub: Hub, args: argparse.Namespace) -> dict[str, Callable[[], Any]]:
    bench = BenchService(hub.settings, hub.slurm)
    human = Actor(kind="human", client="schub-cli")
    return {
        "bench-run": lambda: bench.run(args.project, read_arg(args.code), args.why, args.expect,
                                       checks=_checks(args.checks),
                                       setup=args.setup, actor=human, wait_s=args.wait),
        "bench-wait": lambda: bench.wait(args.ref, args.wait),
        "bench-interrupt": lambda: bench.interrupt(args.ref),
        "bench-status": lambda: bench.status(),
        "bench-stop": lambda: bench.stop_workbench(),
        "bench-watchdog": lambda: check(hub.settings, hub.slurm),
        "bench-journal": lambda: [e.model_dump(mode="json") for e in
                                  bench.journal(args.project).entries(since=args.since, limit=args.limit)],
    }
