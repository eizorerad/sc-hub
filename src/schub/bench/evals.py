"""Breadth evaluation: every request of evals/requests.yaml as a lab-agent goal, once per
engine, scored from the project's journal, the goal's events and the tool-call log.

    schub eval-run evals/requests.yaml --engines claude,codex [--only k562-table-qc,paper-gears]
    schub eval-score evals/requests.yaml [--markdown]

A run is a fresh project (ev-<request>-<engine><time>) whose lab agent gets the
request as its objective, with the engine pinned in goal.md (within the owner's
policy). The score says whether the work was handed over as complete, which of the
request's expectations the journal shows (downloads, Slurm jobs, passed checks, note
kinds, files), what went wrong (checks still failing, lost cells, incidents, tool
errors, usage limits, timeouts), whether a human stepped in, and the turns and cost.
The matrix request x engine is what the owner's engine-policy defaults rest on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import yaml
from pydantic import Field, ValidationError

from ..config import Settings
from ..projects import ProjectStore
from ..slurm import Slurm
from ..state import Frozen
from . import goal_agent
from .checkpoint import CheckpointStore
from .goal import Goal
from .journal import Journal
from .models import CellEntry, NoteEntry

ENGINES = ("claude", "codex")
KERNEL_NOTICE = "The kernel stopped"  # the workbench's lifecycle notice to the agent, not a problem of the run
SUFFIX = ("\n\nWork in this project only, through the sc-hub MCP tools. Resources for this request: at most "
          "{gpu_minutes} GPU-minutes and {download_gb} GB of downloads in total; if the request needs more, say what it "
          "would take and hand over as \"blocked\" instead. When the request is answered, record the findings with the "
          "cells they come from and hand over with disposition \"complete\"; if you need the student, hand over as "
          "\"blocked\" with the question.")


class EvalError(ValueError):
    pass


class Expect(Frozen):
    downloads: int = Field(0, ge=0)
    slurm_jobs: int = Field(0, ge=0)
    checks: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


class EvalRequest(Frozen):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,29}$")
    source: str
    prompt: str = Field(min_length=20, max_length=4000)
    expect: Expect = Expect()
    max_turns: int = Field(6, ge=1, le=50)


def load_requests(path: Path) -> list[EvalRequest]:
    try:
        data = yaml.safe_load(path.read_text())
        requests = [EvalRequest.model_validate(r) for r in (data or {}).get("requests", [])]
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise EvalError(f"{path}: {exc}") from exc
    ids = [r.id for r in requests]
    if len(set(ids)) != len(ids):
        raise EvalError(f"{path}: request ids repeat")
    if not requests:
        raise EvalError(f"{path} has no requests")
    return requests


def runs_path(settings: Settings) -> Path:
    return settings.bench_dir / "evals" / "runs.jsonl"


def launch(settings: Settings, slurm: Slurm, requests: Sequence[EvalRequest], engines: Sequence[str],
           slice_minutes: int = 45, max_turns: int | None = None, gpu_minutes: int = 30,
           download_gb: int = 5, report: bool = False) -> list[dict]:
    unknown = set(engines) - set(ENGINES)
    if unknown:
        raise EvalError(f"unknown engines {sorted(unknown)}")
    stamp = datetime.now(timezone.utc).strftime("%d%H%M")
    launched = []
    for request in requests:
        for engine in engines:
            project = f"ev-{request.id}"[:30].rstrip("-") + f"-{engine[:2]}{stamp}"
            ProjectStore(settings).create(project, question=request.prompt.split(". ")[0][:200])
            turns = min(request.max_turns, max_turns) if max_turns else request.max_turns
            suffix = SUFFIX.format(gpu_minutes=gpu_minutes, download_gb=download_gb)
            goal = (f"---\nengine: {engine}\nmax_turns: {turns}\nslice_minutes: {slice_minutes}\n"
                    f"pace_minutes: 5\nreport: {'yes' if report else 'no'}\n---\n{request.prompt}{suffix}\n")
            job = goal_agent.start(settings, slurm, project, goal)
            record = {"request": request.id, "engine": engine, "project": project, "job": job,
                      "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            runs_path(settings).parent.mkdir(parents=True, exist_ok=True)
            with runs_path(settings).open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            launched.append(record)
    return launched


def score(settings: Settings, project: str, request: EvalRequest) -> dict:
    folder = settings.projects_dir / project
    entries = Journal(folder, project).entries()
    cells = [e for e in entries if isinstance(e, CellEntry)]
    notes = [e for e in entries if isinstance(e, NoteEntry)]
    latest: dict[str, str] = {}
    for cell in cells:
        for result in cell.check_results:
            latest[result.name] = result.status
    passed = {name for name, status in latest.items() if status == "pass"}  # as the work ended, not ever
    kinds = {n.kind for n in notes}
    expect = request.expect
    met = {
        "downloads": sum(d.status == "ok" for c in cells for d in c.downloads) >= expect.downloads,
        "slurm_jobs": sum(len(c.jobs) for c in cells) >= expect.slurm_jobs,
        "checks": set(expect.checks) <= passed,
        "notes": set(expect.notes) <= kinds,
        "files": all(any(folder.glob(pattern)) for pattern in expect.files),
    }
    events = Goal(settings, project).events(limit=10_000)
    ended = [e for e in events if e.get("event") == "turn_finished"]
    finished = [e for e in ended if e.get("role", "research") != "writer"]  # the report is not the request's work
    disposition = CheckpointStore(folder).read().disposition
    return {
        "request": request.id, "project": project, "complete": disposition == "complete", "disposition": disposition,
        "expectations_met": sum(met.values()), "expectations": len(met), "missing": [k for k, v in met.items() if not v],
        "cells": len(cells), "cell_errors": sum(c.status == "error" for c in cells),
        "lost_cells": sum(c.status in ("lost", "retired") for c in cells),
        "checks_failing": sorted(k for k, v in latest.items() if v in ("fail", "error")),
        "incidents": sum(n.kind == "incident" and not n.text.startswith(KERNEL_NOTICE) for n in notes),
        "own_errors": sum(n.kind == "error" for n in notes),
        "human_steps": sum(e.actor.kind == "human" for e in entries),
        "engines": sorted({e["engine"] for e in finished}),
        "turns": sum(_worked(e) for e in finished),
        "timeouts": sum(e.get("status") == "timed_out" for e in finished),
        "usage_limits": sum(e.get("status") == "usage_limited" for e in finished),
        "cost_usd": round(sum(e.get("cost_usd") or 0 for e in finished), 2),
        "tool_errors": _tool_errors(settings, project),
        "report_turns": sum(_worked(e) for e in ended if e.get("role") == "writer"),
    }


def _worked(event: dict) -> bool:
    """The lab agent's own rule (goal_agent.did_work), from a turn_finished event."""
    if event.get("status") in ("ok", "timed_out", "failed"):
        return True
    return event.get("status") == "usage_limited" and ((event.get("turns") or 0) > 1 or bool(event.get("cost_usd")))


def _tool_errors(settings: Settings, project: str) -> int:
    errors = 0
    for path in sorted(settings.logs_dir.glob("calls-*.jsonl")) if settings.logs_dir.is_dir() else []:
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            args = record.get("args") or {}
            if args.get("project") == project or str(args.get("ref", "")).startswith(project + "#"):
                errors += not record.get("ok", True)
    return errors


def matrix(settings: Settings, requests: Sequence[EvalRequest]) -> list[dict]:
    """The latest run of each request x engine, scored."""
    by_id = {r.id: r for r in requests}
    latest: dict[tuple[str, str], dict] = {}
    try:
        lines = runs_path(settings).read_text().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        run = json.loads(line)
        if run["request"] in by_id:
            latest[(run["request"], run["engine"])] = run
    rows = []
    for (request_id, engine), run in sorted(latest.items()):
        if (settings.projects_dir / run["project"]).is_dir():
            rows.append({"engine": engine, **score(settings, run["project"], by_id[request_id])})
    return rows


def markdown(rows: Sequence[dict]) -> str:
    lines = ["| request | engine | complete | expectations | failing checks | problems | turns | cost $ |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        problems = ", ".join(f"{k} {r[k]}" for k in ("cell_errors", "lost_cells", "incidents", "tool_errors",
                                                    "usage_limits", "timeouts", "human_steps") if r[k])
        lines.append(f"| {r['request']} | {r['engine']} | {'yes' if r['complete'] else r['disposition']} | "
                     f"{r['expectations_met']}/{r['expectations']}{' (missing ' + ', '.join(r['missing']) + ')' if r['missing'] else ''} | "
                     f"{', '.join(r['checks_failing']) or '-'} | {problems or '-'} | {r['turns']} | {r['cost_usd']} |")
    return "\n".join(lines)
