"""CLI commands for projects, branches, ideas, the logbook, assets and the dashboard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

import yaml

from .projects import BranchSpec
from .service import Hub


def _load_spec(raw: str) -> BranchSpec:
    path = Path(raw)
    text = path.read_text() if path.is_file() else raw
    data = yaml.safe_load(text)  # YAML is a superset of JSON
    if not isinstance(data, dict):
        raise ValueError("branch spec must be a mapping (YAML or JSON)")
    return BranchSpec.model_validate(data)


def add_parsers(sub: Any) -> None:
    new = sub.add_parser("project-new", help="create a project or subproject (a/b)")
    new.add_argument("project")
    new.add_argument("--question", default="")
    new.add_argument("--dataset", action="append", default=[])
    sub.add_parser("projects", help="list projects with branches, ideas and logbook")
    save = sub.add_parser("branch-save", help="save a branch (YAML/JSON spec or file) and dry-run plan it")
    save.add_argument("project")
    save.add_argument("name")
    save.add_argument("spec")
    save.add_argument("--overwrite", action="store_true")
    plan = sub.add_parser("branch-plan", help="plan a saved branch (then: schub submit <plan_id>)")
    plan.add_argument("project")
    plan.add_argument("name")
    idea = sub.add_parser("idea-new", help="add an idea to a project")
    idea.add_argument("project")
    idea.add_argument("slug")
    idea.add_argument("--title", required=True)
    idea.add_argument("--hypothesis", default="")
    idea.add_argument("--reverses-if", default="")
    update = sub.add_parser("idea-set", help="change an idea's status or linked branches")
    update.add_argument("project")
    update.add_argument("slug")
    update.add_argument("--status", choices=["open", "planned", "running", "done", "dropped"])
    update.add_argument("--branch", action="append", default=[])
    log = sub.add_parser("log", help="append a logbook entry")
    log.add_argument("project")
    log.add_argument("text")
    assets = sub.add_parser("assets", help="where starter assets are found; --missing lists absent ones")
    assets.add_argument("names", nargs="*", default=["pbmc3k", "kang2018", "celltypist"])
    assets.add_argument("--missing", action="store_true")
    dash = sub.add_parser("dashboard", help="write the static dashboard (view/)")
    dash.add_argument("--out", type=Path)


def _idea_changes(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.status:
        changes["status"] = args.status
    if args.branch:
        changes["branches"] = tuple(args.branch)
    return changes


def _assets(hub: Hub, args: argparse.Namespace) -> Any:
    from .fetch import missing
    from .library import find_asset

    if args.missing:
        return " ".join(missing(hub.settings, args.names))
    return {n: (found.model_dump() if (found := find_asset(hub.settings, n)) else None) for n in args.names}


def handlers(hub: Hub, args: argparse.Namespace) -> dict[str, Callable[[], Any]]:
    from .dashboard import build_dashboard

    return {
        "project-new": lambda: hub.create_project(args.project, args.question, args.dataset),
        "projects": lambda: [json.loads(p.model_dump_json()) for p in hub.list_projects()],
        "branch-save": lambda: hub.save_branch(args.project, args.name, _load_spec(args.spec), args.overwrite).summary(),
        "branch-plan": lambda: hub.plan_branch(args.project, args.name).summary(),
        "idea-new": lambda: hub.add_idea(args.project, args.slug, args.title, args.hypothesis, args.reverses_if),
        "idea-set": lambda: hub.update_idea(args.project, args.slug, **_idea_changes(args)),
        "log": lambda: hub.add_log(args.project, args.text),
        "assets": lambda: _assets(hub, args),
        "dashboard": lambda: build_dashboard(hub, args.out),
    }
