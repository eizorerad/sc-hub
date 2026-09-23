"""One step of a branch as the assistant needs it: what it is, what it did, and
how to change it. Addressed like the dashboard shows it: '<project>/<branch>#<step>'."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .headlines import headline
from .planner import Plan
from .slurm import ACTIVE_STATES
from .state import Frozen
from .stepfile import ERROR_FILE, JOB_ID_FILE, SUCCESS, SUMMARY_FILE

MAX_FILES = 30
HOW_TO_CHANGE = (
    "To fix this step in place (same branch, next revision): revise_branch(project, branch, step, "
    "params={...} or brick=..., reason=...). To try an alternative from this step on without touching "
    "the branch: fork_branch(project, branch, step, new_branch, params/brick/then, reason). Both return "
    "a dry-run plan; unchanged earlier steps are reused from cache."
)


class StepDetail(Frozen):
    ref: str
    project: str
    branch: str
    revision: int | None
    index: int
    brick: str
    params: dict[str, Any]
    step_key: str
    state: str
    headline: str = ""
    message: str | None = None
    summary: dict[str, Any] | None = None
    files: tuple[str, ...] = ()
    log_tail: tuple[str, ...] = ()
    runs: tuple[str, ...] = ()
    how_to_change: str = HOW_TO_CHANGE


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _state(hub: Any, step_dir: Path) -> tuple[str, str | None]:
    if (step_dir / SUCCESS).exists():
        return "COMPLETED", None
    error = _read_json(step_dir / ERROR_FILE)
    if error is not None:
        return "FAILED", str(error.get("message", ""))[:500]
    job = step_dir / JOB_ID_FILE
    if job.is_file():
        job_id = job.read_text().strip()
        state = hub.slurm.states([job_id]).get(job_id, "")
        return (state, None) if state in ACTIVE_STATES else ("STOPPED", "the job ended without a result")
    return "NOT_RUN", None


def step_detail(hub: Any, ref: str, plan: Plan, index: int) -> StepDetail:
    from .dashboard.steps import step_extras

    step = next(s for s in plan.steps if s.index == index)  # invalid steps are left out of plans
    step_dir = hub.settings.steps_dir / step.step_key
    state, message = _state(hub, step_dir)
    summary = _read_json(step_dir / "results" / SUMMARY_FILE) if state == "COMPLETED" else None
    results = step_dir / "results"
    files = tuple(sorted(str(p) for p in results.iterdir() if p.is_file())[:MAX_FILES]) if results.is_dir() else ()
    runs = tuple(
        m.run_id for m in hub.store.list_runs(100) if any(s.step_key == step.step_key for s in m.steps)
    )
    return StepDetail(
        ref=ref, project=plan.project or "", branch=plan.branch or "", revision=plan.revision,
        index=index, brick=step.brick, params=step.params, step_key=step.step_key, state=state,
        headline=headline(step.brick, summary), message=message, summary=summary, files=files,
        log_tail=step_extras(step_dir, with_log=True).log_tail if step_dir.is_dir() else (), runs=runs,
    )
