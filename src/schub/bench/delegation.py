"""Handing a task to the lab agent from a chat: the `delegate` and `delegation` MCP tools.

The student's own assistant (on the laptop, or any chat) shapes the task with the student,
then hands it over; the lab agent on the cluster works on it alone in Slurm slices (goal_agent),
free to use its own tools in the project folder (mode: free) or only the bench's (mode: bench).
Everything it does lands in the project's journal, so the chat can follow it with
`delegation(project)` or the dashboard, and stop it at any time.
"""

from __future__ import annotations

from typing import Any, Literal

from ..config import Settings
from ..slurm import Slurm
from ..state import Frozen
from . import goal_agent
from .clock import stamp
from .goal import GoalError
from .models import Actor

MAX_TURNS = 100


class DelegationError(ValueError):
    pass


class DelegationAnswer(Frozen):
    project: str
    state: str  # started | queued | working | waiting | complete | blocked | stopped | not started
    job: str = ""  # the next slice's Slurm job
    turns: int = 0
    max_turns: int = 0
    engine: str = ""
    mode: str = ""
    next_action: str = ""
    slices: tuple[dict[str, Any], ...] = ()
    recent: tuple[str, ...] = ()  # the last things the lab agent did
    report: str = ""
    hint: str = ""


def goal_text(objective: str, deliverables: str, max_turns: int, engine: str, mode: str, report: bool) -> str:
    """goal.md; the hand-over's time is a comment of the settings, so the same task handed over again is the
    same objective (its budget and sessions stay)."""
    body = objective.strip()
    if deliverables.strip():
        body += f"\n\nDeliverables: {deliverables.strip()}"
    return (f"---\n# handed over by the student's assistant through delegate() on {stamp()[:16]} UTC\n"
            f"engine: {engine}\nmax_turns: {max_turns}\nmode: {mode}\nreport: {'yes' if report else 'no'}\n"
            f"---\n{body}\n")


def delegate(settings: Settings, slurm: Slurm, project: str, objective: str, deliverables: str = "",
             max_turns: int = 20, engine: Literal["auto", "claude", "codex"] = "auto",
             mode: Literal["free", "bench"] = "free", report: bool = True, actor: Actor | None = None,
             replace: bool = False) -> DelegationAnswer:
    if actor is not None and actor.kind == "lab_agent":
        raise DelegationError("a lab agent never hands work to another lab agent")
    _project(settings, project)
    current = delegation(settings, slurm, project)
    if current.state in ("queued", "working", "waiting") and not replace:
        raise DelegationError(f"the lab agent is already working on {project} ({current.turns} of {current.max_turns} "
                              "turns): follow it with delegation(), stop it first, or delegate(..., replace=true)")
    if not 1 <= max_turns <= MAX_TURNS:
        raise DelegationError(f"max_turns must be from 1 to {MAX_TURNS}")
    if len(objective.strip()) < 20:
        raise DelegationError("write the objective in full: the question, the data, what counts as done")
    try:
        job = goal_agent.start(settings, slurm, project, goal_text(objective, deliverables, max_turns, engine, mode,
                                                                   report))
    except GoalError as exc:
        raise DelegationError(str(exc)) from exc
    answer = delegation(settings, slurm, project)
    return answer.model_copy(update={"state": "started", "job": job, "hint": (
        "The lab agent works on its own now, in Slurm slices about an hour apart; each turn lands in the journal. "
        "Follow it with delegation(project) or journal(project), or on the dashboard; stop it with "
        "delegation(project, stop=true).")})


def _project(settings: Settings, project: str) -> None:
    from ..projects import ProjectError, ProjectStore

    try:
        ProjectStore(settings).require(project)
    except ProjectError as exc:
        raise DelegationError(str(exc)) from exc


def delegation(settings: Settings, slurm: Slurm, project: str, stop: bool = False,
               actor: Actor | None = None) -> DelegationAnswer:
    _project(settings, project)
    if stop and actor is not None and actor.kind == "lab_agent":
        raise DelegationError("a lab agent does not stop lab agents; hand over as blocked to end your own work")
    if stop:
        goal_agent.stop(settings, project)
    status = goal_agent.status(settings, slurm, project)
    config, checkpoint = status.get("goal") or {}, status.get("checkpoint") or {}
    if "error" in config or not (settings.projects_dir / project / "goal" / "goal.md").exists():
        return DelegationAnswer(project=project, state="not started", hint="Nothing was handed over for this "
                                "project; delegate() does it.")
    slices = tuple({k: s.get(k) for k in ("job_id", "state", "start", "reason")} for s in status.get("slices") or ()
                   if "job_id" in s)
    disposition = str(checkpoint.get("disposition", ""))
    running = any(s.get("state") == "RUNNING" for s in slices)
    state = "stopped" if status.get("stopped") else (
        disposition if disposition in ("complete", "blocked", "waiting") else
        ("working" if running else "queued" if slices else "idle"))
    recent = tuple(_event(e) for e in (status.get("events") or ())[-6:])
    report = status.get("report") or {}
    return DelegationAnswer(
        project=project, state=state, job=str(slices[0]["job_id"]) if slices else "", turns=int(status.get("turns", 0)),
        max_turns=int(config.get("max_turns", 0)), engine=str(config.get("engine", "")),
        mode=str(config.get("mode", "")), next_action=str(checkpoint.get("next_action", ""))[:500], slices=slices,
        recent=tuple(r for r in recent if r), report=str(report.get("status", "")),
        hint="stopped: slices already queued exit at once" if status.get("stopped") else "")


def _event(event: dict) -> str:
    kind = str(event.get("event", ""))
    if kind in ("turn_finished", "incident", "stuck_jobs", "long_wait", "all_paused", "budget", "terminal",
                "engine_failing", "signed_in_again", "withdrawn", "report_published", "own_work_recorded"):
        detail = {k: v for k, v in event.items() if k in ("engine", "status", "text", "jobs", "cell", "disposition")}
        return f"{str(event.get('at', ''))[:16]} {kind} {detail}"[:300]
    return ""
