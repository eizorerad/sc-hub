"""One row per branch for the Experiments table: what it is (datasets, steps, the
parameters of every step), where it stands, its key numbers, labels and sweep.

The page gets these rows as JSON and filters, sorts, groups and compares them in
the browser, so hundreds of branches stay one small table instead of a graph.
"""

from __future__ import annotations

import re
from typing import Any

from ..state import Frozen
from .collect import BRANCH_LABELS, BranchInfo, NodeView, Snapshot, run_label
from .lineage import SHORT
from .metrics import metric_catalog, metrics_of
from .views_runs import ImageUrl

STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


class ExperimentStep(Frozen):
    i: int
    brick: str
    short: str
    key: str
    state: str
    params: dict[str, Any] = {}
    head: str = ""
    shared: bool = False  # the same step (key) is used by another branch too
    thumbs: tuple[str, ...] = ()


class Experiment(Frozen):
    id: str  # "<project>/<branch>"
    view: str | None  # the branch's graph in Pipelines
    project: str
    branch: str
    rev: int
    desc: str = ""
    idea: str | None = None
    origin: str = ""
    datasets: tuple[str, ...] = ()
    state: str
    state_label: str
    steps: tuple[ExperimentStep, ...] = ()
    metrics: dict[str, float | int] = {}
    sources: dict[str, str] = {}  # metric key -> the step (key) that gave it
    stale: tuple[str, ...] = ()  # metric keys taken from an older version of the branch
    issues: tuple[dict[str, Any], ...] = ()
    tags: tuple[str, ...] = ()
    pinned: bool = False
    archived: bool = False
    sweep: dict[str, Any] | None = None
    updated: str = ""  # "YYYY-MM-DDTHH:MM" (UTC)
    run: str | None = None  # latest run id
    problem: str = ""


def _stamp(text: str) -> str:
    found = STAMP.search(text or "")
    return f"{found.group(1)}T{found.group(2)}" if found else ""


def _origin(info: BranchInfo) -> str:
    if info.forked_from:
        parent, _, step = info.forked_from.partition("#")
        return f"fork of {parent} at step {step}"
    return f"variant of {info.from_branch}" if info.from_branch else ""


def _steps(info: BranchInfo, snap: Snapshot, nodes: dict[str, NodeView], usage: dict[str, int],
           image_url: ImageUrl) -> tuple[list[ExperimentStep], list[tuple[str, str, Any]], list[tuple[str, str, Any]]]:
    """The branch's steps as they are now, (brick, key, summary) of each, and of its earlier results."""
    label = f"{info.project}/{info.name}"
    steps, now, before = [], [], []
    for index, key in enumerate(info.keys, start=1):
        node = nodes.get(key)
        if node is None:
            continue
        view = snap.steps_by_key.get(key)
        thumbs = tuple(u for name in (view.extras.figures if view else ()) if (u := image_url(view, name, False)))
        steps.append(ExperimentStep(i=index, brick=node.brick, short=SHORT.get(node.brick, node.brick), key=key,
                                    state="OUTDATED" if label in node.earlier else node.state, params=node.params,
                                    head=node.headline, shared=usage.get(key, 0) > 1, thumbs=thumbs))
        now.append((node.brick, key, view.summary if view else None))
        earlier = snap.steps_by_key.get(node.earlier.get(label, ""))
        before.append((node.brick, earlier.key if earlier else "", earlier.summary if earlier else None))
    return steps, now, before


def _updated(info: BranchInfo, snap: Snapshot, steps: list[ExperimentStep], latest_run: Any) -> str:
    stamps = [_stamp(info.saved)]
    if latest_run is not None:
        stamps.append(_stamp(latest_run.created_at))
    stamps += [_stamp(v.extras.finished) for s in steps if (v := snap.steps_by_key.get(s.key)) is not None]
    return max((s for s in stamps if s), default="")


def experiment(info: BranchInfo, snap: Snapshot, nodes: dict[str, NodeView], usage: dict[str, int],
               views: dict[str, str], latest: dict[str, Any], image_url: ImageUrl) -> Experiment:
    label = f"{info.project}/{info.name}"
    steps, now, before = _steps(info, snap, nodes, usage, image_url)
    (current, sources), (older, older_sources) = metrics_of(now), metrics_of(before)
    stale = tuple(k for k in older if k not in current)
    issues = {(i.level, i.code, i.message): {"level": i.level, "code": i.code, "step": i.step, "text": i.message}
              for key in info.keys if key in nodes for i in nodes[key].issues}
    sweep = ({"name": info.sweep, "step": info.sweep_step, "param": info.sweep_param, "value": info.sweep_value}
             if info.sweep else None)
    return Experiment(
        id=label, view=views.get(label), project=info.project, branch=info.name, rev=info.revision,
        desc=info.description, idea=info.idea, origin=_origin(info), datasets=info.datasets, state=info.state,
        state_label=BRANCH_LABELS.get(info.state, info.state.lower()), steps=tuple(steps),
        metrics={**{k: older[k] for k in stale}, **current}, stale=stale, issues=tuple(issues.values()),
        sources={**{k: older_sources[k] for k in stale}, **sources},
        tags=info.label.tags, pinned=info.label.pinned, archived=info.label.archived, sweep=sweep,
        updated=_updated(info, snap, steps, latest.get(label)), run=getattr(latest.get(label), "run_id", None),
        problem=info.problem,
    )


def experiments(snap: Snapshot, views: dict[str, str], image_url: ImageUrl) -> list[Experiment]:
    nodes = {n.key: n for n in snap.nodes}
    usage: dict[str, int] = {}
    for info in snap.branches.values():
        for key in info.keys:
            usage[key] = usage.get(key, 0) + 1
    latest: dict[str, Any] = {}
    for run in snap.runs:  # newest first
        latest.setdefault(run_label(run), run)
    return [experiment(info, snap, nodes, usage, views, latest, image_url) for info in snap.branches.values()]


def experiments_payload(snap: Snapshot, views: dict[str, str], image_url: ImageUrl) -> dict[str, Any]:
    rows = experiments(snap, views, image_url)
    return {
        "generated": snap.generated_at,
        "today": _stamp(snap.generated_at)[:10],
        "metrics": metric_catalog(),
        "states": BRANCH_LABELS,
        "rows": [r.model_dump(mode="json") for r in rows],
    }
