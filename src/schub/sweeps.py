"""Sweeps: one parameter of one step over several values, as one experiment.

A sweep is a set of forks of a base branch, one per value, named <sweep>-<value>
and tagged with what varies. Shared steps before the varied one are computed once
(same step keys). The dashboard folds a sweep into one row with a small
value -> result table instead of N unrelated branches.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Sequence

from .planner import PlanSummary
from .projects import SEGMENT, BranchSpec
from .queue import QueuedSubmission
from .revisions import forked_spec
from .runs import RunManifest
from .state import Frozen

if TYPE_CHECKING:
    from .service import Hub

MIN_VALUES, MAX_VALUES = 2, 24


class SweepError(ValueError):
    pass


class SweepResult(Frozen):
    project: str
    sweep: str
    base: str
    step: int
    param: str
    branches: tuple[str, ...]
    plans: tuple[PlanSummary, ...]


def value_slug(value: Any) -> str:
    """0.5 -> '0-5', 'Immune_All_High.pkl' -> 'immune_all_high-pkl', True -> 'true'."""
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_]+", "-", text).strip("-")[:20] or "value"


def _names(sweep: str, values: Sequence[Any]) -> list[str]:
    if not SEGMENT.fullmatch(sweep):
        raise SweepError(f"invalid sweep name '{sweep}': lowercase letters, digits, '-' or '_'")
    if not MIN_VALUES <= len(values) <= MAX_VALUES:
        raise SweepError(f"a sweep needs at least {MIN_VALUES} and at most {MAX_VALUES} values")
    names = [f"{sweep}-{value_slug(v)}" for v in values]
    if len(set(names)) != len(names):
        raise SweepError(f"two values give the same branch name ({names}); use distinct values")
    bad = [n for n in names if not SEGMENT.fullmatch(n)]
    if bad:
        raise SweepError(f"branch names too long or invalid: {bad}; use a shorter sweep name")
    return names


def create_sweep(hub: Hub, project: str, branch: str, step: int, param: str, values: Sequence[Any],
                 sweep: str, reason: str) -> SweepResult:
    """All variants are checked (dry-run plans) before any is saved: all or nothing."""
    if not reason.strip():
        raise SweepError("give a reason: it is kept with every branch of the sweep")
    names = _names(sweep, values)
    taken = [n for n in names if hub.projects.branch_exists(project, n)]
    if taken:
        raise SweepError(f"{', '.join(taken)} already exists in '{project}'; choose another sweep name")
    specs = []
    for name, value in zip(names, values):
        spec = forked_spec(hub.projects, project, branch, step, {param: value}, None, None, reason.strip())
        spec = spec.model_copy(update={"sweep": sweep, "sweep_step": step, "sweep_param": param, "sweep_value": value,
                                       "description": f"sweep {sweep}: step {step} {param}={value}"})
        errors = hub.dry_run_errors(project, name, spec)
        if errors:
            raise SweepError(f"{param}={value!r}: {errors}")
        specs.append((name, spec))
    plans = tuple(hub.save_checked_branch(project, name, spec, reason.strip()).summary() for name, spec in specs)
    return SweepResult(project=project, sweep=sweep, base=branch, step=step, param=param,
                       branches=tuple(names), plans=plans)


def members(hub: Hub, project: str, sweep: str) -> list[str]:
    """Branches of a sweep, in the order of their values."""
    found: list[tuple[int, str, BranchSpec]] = []
    for name in hub.projects.branches(project):
        spec = hub.projects.load_branch(project, name)
        if spec.sweep == sweep:
            found.append((0 if isinstance(spec.sweep_value, (int, float)) else 1, name, spec))
    found.sort(key=lambda item: (item[0], item[2].sweep_value if item[0] == 0 else str(item[2].sweep_value)))
    return [name for _, name, _ in found]


def submit_sweep(hub: Hub, project: str, sweep: str) -> list[RunManifest | QueuedSubmission]:
    names = members(hub, project, sweep)
    if not names:
        raise SweepError(f"no sweep '{sweep}' in '{project}'")
    return [hub.submit(hub.plan_branch(project, name).plan_id) for name in names]
