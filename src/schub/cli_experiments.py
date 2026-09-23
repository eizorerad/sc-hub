"""CLI commands for many experiments: sweeps, sc-hub's submission queue, branch labels."""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from .service import Hub


def add_experiment_parsers(sub: Any) -> None:
    sub.add_parser("pump", help="submit queued plans that fit now (the queue's Slurm pump runs this)")
    sub.add_parser("queue", help="plans waiting in sc-hub's queue, and those that could not be submitted")
    sub.add_parser("queue-cancel", help="remove a plan from sc-hub's queue").add_argument("plan_id")
    sweep = sub.add_parser("sweep", help="one branch per value of one step's parameter (a sweep)")
    for name in ("project", "branch"):
        sweep.add_argument(name)
    sweep.add_argument("step", type=int)
    sweep.add_argument("param")
    sweep.add_argument("values", help='JSON list, e.g. [0.5, 1.0, 2.0]')
    sweep.add_argument("--name", required=True, help="sweep name; branches are <name>-<value>")
    sweep.add_argument("--reason", required=True)
    submit = sub.add_parser("sweep-submit", help="submit every branch of a sweep (the rest waits in the queue)")
    submit.add_argument("project")
    submit.add_argument("sweep")
    label = sub.add_parser("branch-label", help="tag, pin or archive a branch (no new revision)")
    label.add_argument("project")
    label.add_argument("branch")
    label.add_argument("--tag", action="append", default=[], help="add a tag (repeatable)")
    label.add_argument("--untag", action="append", default=[], help="remove a tag (repeatable)")
    for flag in ("pin", "archive"):
        group = label.add_mutually_exclusive_group()
        group.add_argument(f"--{flag}", dest=flag, action="store_true", default=None)
        group.add_argument(f"--un{flag}", dest=flag, action="store_false")


def experiment_handlers(hub: Hub, args: argparse.Namespace) -> dict[str, Callable[[], Any]]:
    return {
        "pump": lambda: hub.pump(),
        "queue": lambda: {"waiting": hub.queued(), "failed": hub.queue_failures()},
        "queue-cancel": lambda: hub.cancel_queued(args.plan_id),
        "sweep": lambda: hub.sweep_branch(args.project, args.branch, args.step, args.param, json.loads(args.values),
                                          args.name, args.reason),
        "sweep-submit": lambda: hub.submit_sweep(args.project, args.sweep),
        "branch-label": lambda: hub.label_branch(args.project, args.branch, args.tag, args.untag,
                                                 getattr(args, "pin", None), getattr(args, "archive", None)),
    }
