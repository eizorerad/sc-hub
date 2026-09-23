"""Projects: a tree of projects and subprojects on the left, one project at a time on
the right (question, input datasets, branches with revisions and forks, ideas,
logbook). Scales to many projects without burying anything below a long list."""

from __future__ import annotations

from ..projects import ProjectSummary
from .collect import BranchInfo, RunView, Snapshot
from .html import esc, pill, table
from .lineage import view_id
from .notebooks import notebook_button

IDEA_COLUMNS = ("open", "planned", "running", "done", "dropped")
IDEA_STATE = {"open": "PENDING", "planned": "PLANNED", "running": "RUNNING", "done": "COMPLETED", "dropped": "FAILED"}
PROJECT_STATE = {"active": "RUNNING", "paused": "PENDING", "done": "COMPLETED"}


def _idea_card(idea) -> str:
    hypothesis = f'<div class="muted small">{esc(idea.hypothesis)}</div>' if idea.hypothesis else ""
    branches = f'<div class="small">branches: {esc(", ".join(idea.branches))}</div>' if idea.branches else ""
    return f'<div class="idea" title="{esc(idea.hypothesis)}"><b>{esc(idea.title)}</b>{hypothesis}{branches}</div>'


def _ideas(project: ProjectSummary) -> str:
    if not project.ideas:
        return '<p class="muted small">No ideas yet. Ask your assistant to note one when a question comes up.</p>'
    columns = []
    for status in IDEA_COLUMNS:
        items = [i for i in project.ideas if i.status == status]
        if not items and status == "dropped":
            continue
        cards = "".join(_idea_card(i) for i in items)
        columns.append(f"<div class=column>{pill(IDEA_STATE[status], status)}{cards or '<p class=empty>none</p>'}</div>")
    return f'<div class="board">{"".join(columns)}</div>'


def _origin(info: BranchInfo) -> str:
    if info.forked_from:
        parent, _, step = info.forked_from.partition("#")
        return f'<span class="tag">fork</span> of <code>{esc(parent)}</code> at step {esc(step)}'
    if info.from_branch:
        return f'variant of <code>{esc(info.from_branch)}</code>'
    return '<span class="muted">own steps</span>'


def _history(info: BranchInfo) -> str:
    if len(info.history) <= 1:
        return '<span class="muted small">r1 only</span>'
    items = "".join(
        f"<li><b>r{r.revision}</b> <span class='muted small'>{esc(r.saved)}</span>"
        f"{' · ' + esc(r.reason) if r.reason else ''}<ul class=plain>"
        + "".join(f"<li class='small'>{esc(c)}</li>" for c in r.changes) + "</ul></li>"
        for r in reversed(info.history)
    )
    return f"<details><summary>{len(info.history)} revisions</summary><ul class='plain history'>{items}</ul></details>"


def _branch_rows(project: ProjectSummary, snap: Snapshot, runs: tuple[RunView, ...]) -> list[str]:
    latest: dict[str, RunView] = {}
    for run in runs:
        if run.project == project.path and run.branch and run.branch not in latest:
            latest[run.branch] = run
    rows = []
    for name in project.branches:
        info = snap.branches.get(f"{project.path}/{name}") or BranchInfo(project=project.path, name=name)
        run = latest.get(name)
        state = pill(run.state) if run else pill("PLANNED", "not run yet")
        # Stale if the branch as it is now (also through a revised parent) has other steps.
        stale = run is not None and bool(info.keys) and bool(run.steps) and run.steps[-1].key not in info.keys
        run_cell = (f'<a href="#runs/{esc(run.run_id)}">r{esc(run.revision or "?")} run</a>'
                    + (' <span class="tag">branch changed since</span>' if stale else "")) if run else ""
        problem = f'<div class="note bad small">{esc(info.problem)}</div>' if info.problem else ""
        rows.append(
            f"<tr><td><code>{esc(name)}</code> <span class='tag'>r{info.revision}</span>{problem}"
            f"<div class='muted small'>{esc(info.description)}</div></td>"
            f"<td>{_origin(info)}</td><td>{esc(', '.join(info.datasets))}</td>"
            f"<td>{state} {run_cell}</td><td>{_history(info)}</td>"
            f'<td class="open"><a href="#pipelines/{esc(view_id(project.path + "/" + name))}">graph</a>'
            f"{notebook_button(run, snap.notebooks, 'notebook') if run else ''}</td></tr>"
        )
    return rows


def _inputs(project: ProjectSummary, snap: Snapshot) -> str:
    used = {d for key, info in snap.branches.items() if info.project == project.path for d in info.datasets}
    names = sorted(used | set(project.meta.datasets))
    chips = "".join(f'<a class="chip" href="#library">{esc(n)}</a>' for n in names)
    return f'<div class="chips"><span class="muted small">Datasets</span>{chips or "<span class=muted>none yet</span>"}</div>'


def _software(project: ProjectSummary, snap: Snapshot) -> str:
    env = snap.kernels.get(project.path)
    building = project.path in snap.env_builds
    status = pill("RUNNING", "building") if building else ""
    if env is None:
        return (f'<div class="chips"><span class="muted small">Software</span><span class="chip">shared sc-hub environment</span>'
                f"{status}</div>")
    chips = "".join(f'<span class="chip">{esc(e)}</span>' for e in (*env.pip, *(f"{c} (conda)" for c in env.conda)))
    return (f'<div class="chips"><span class="muted small">Software</span><span class="chip">shared environment</span>'
            f'<span class="muted small">+</span>{chips}{status or pill("COMPLETED", "kernel ready")}'
            f'<span class="muted small">Jupyter kernel "sc-hub: {esc(project.path)}" · built {esc(env.built)}</span></div>')


def _project(project: ProjectSummary, snap: Snapshot, children: list[str]) -> str:
    rows = _branch_rows(project, snap, snap.runs)
    log = "".join(f"<pre class=entry>{esc(e)}</pre>" for e in reversed(project.logbook_tail)) or '<p class="empty">Empty.</p>'
    problems = "".join(f'<p class="note bad">{esc(p)}</p>' for p in project.problems)
    subs = "".join(f'<button type="button" class="chip" data-project-link="{esc(c)}">{esc(c)}</button>' for c in children)
    return (
        f'<div class="card project" data-project="{esc(project.path)}" hidden><div class="run-title"><h2>{esc(project.path)}</h2>'
        f"{pill(PROJECT_STATE[project.meta.status], project.meta.status)}</div>"
        f'<p class="question">{esc(project.meta.question or "No question written yet.")}</p>'
        + _inputs(project, snap)
        + _software(project, snap)
        + (f'<div class="chips"><span class="muted small">Subprojects</span>{subs}</div>' if subs else "")
        + "<h3>Branches</h3>"
        + (table(("Branch", "Based on", "Datasets", "Latest run", "History", "Open"), rows) if rows
           else '<p class="empty">No branches yet.</p>')
        + '<p class="muted small">A fix makes a new revision of the same branch (r2, r3…); '
          "an alternative from some step on is a fork (a new branch). Ask your assistant from any step's panel.</p>"
        + f"<h3>Ideas</h3>{problems}{_ideas(project)}"
        + f"<details><summary>Logbook, latest first</summary>{log}</details></div>"
    )


def render_projects(snap: Snapshot) -> str:
    if not snap.projects:
        return '<p class="empty">No projects yet. Ask your assistant to create one with your research question.</p>'
    paths = [p.path for p in snap.projects]
    tree = "".join(
        f'<button type="button" class="pipe" data-project-link="{esc(p.path)}" data-text="{esc(p.path.lower() + " " + p.meta.question.lower())}" '
        f'style="padding-left:{8 + 16 * p.path.count("/")}px"><span class="dot {esc(PROJECT_STATE[p.meta.status])}"></span>'
        f'{esc(p.path.rsplit("/", 1)[-1])}<span class="muted small">{len(p.branches)}</span></button>'
        for p in snap.projects
    )
    search = '<input class="tree-filter" type="search" placeholder="Filter projects" aria-label="Filter projects">' if len(paths) > 8 else ""
    cards = "".join(
        _project(p, snap, [c for c in paths if c.rpartition("/")[0] == p.path]) for p in snap.projects
    )
    return (f'<div class="master"><aside class="pipe-list project-tree">{search}{tree}</aside>'
            f'<div class="detail">{cards}</div></div>')
