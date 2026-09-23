"""Projects: a tree of projects and subprojects on the left, one project at a time on
the right: the question, then its branches as a calm list (click one for its graph).
The rest waits behind a '⋯' or a '?': a branch's latest run, notebook and revisions;
the project's software and logbook; what a fix or a fork means. Scales to many
projects without burying anything below a long list."""

from __future__ import annotations

from ..projects import ProjectSummary
from .collect import BRANCH_LABELS, BranchInfo, RunView, Snapshot
from .html import esc, hint, menu, pill
from .lineage import view_id
from .notebooks import notebook_button

RECENT_BRANCHES = 10  # a project lists pinned and recent branches; Compare has them all

IDEA_STATE = {"open": "PENDING", "planned": "PLANNED", "running": "RUNNING", "done": "COMPLETED", "dropped": "FAILED"}
LIVE_IDEAS = ("running", "planned", "open")  # on the page; done and dropped fold away
PROJECT_STATE = {"active": "ACTIVE", "paused": "PENDING", "done": "COMPLETED"}  # active is not a running job
BRANCHES_HINT = ("A branch is one way to analyse the data. Click it for its pipeline and each step's result. "
                 "A fix makes a new revision of the same branch (r2, r3…); an alternative from some step on "
                 "is a new branch. Ask your assistant from any step.")
IDEAS_HINT = "Questions to try, noted by your assistant. Ask it to note one when a question comes up."


def _idea(idea) -> str:
    hypothesis = f'<div class="muted small">{esc(idea.hypothesis)}</div>' if idea.hypothesis else ""
    branches = f'<div class="muted small">branches: {esc(", ".join(idea.branches))}</div>' if idea.branches else ""
    return (f'<li class="idea"><div><b>{esc(idea.title)}</b>{hypothesis}{branches}</div>'
            f"{pill(IDEA_STATE[idea.status], idea.status)}</li>")


def _ideas(project: ProjectSummary) -> str:
    if not project.ideas:
        return ""
    live = [i for status in LIVE_IDEAS for i in project.ideas if i.status == status]
    closed = [i for i in project.ideas if i.status not in LIVE_IDEAS]
    folded = (f'<details class="quiet-details"><summary>{len(closed)} done or dropped</summary>'
              f'<ul class="ideas">{"".join(_idea(i) for i in closed)}</ul></details>') if closed else ""
    return (f'<div class="section-head"><h3>Ideas</h3>{hint(IDEAS_HINT)}</div>'
            + (f'<ul class="ideas">{"".join(_idea(i) for i in live)}</ul>' if live else "") + folded)


def _origin(info: BranchInfo) -> str:
    if info.forked_from:
        parent, _, step = info.forked_from.partition("#")
        return f'<span class="tag">fork</span> of <code>{esc(parent)}</code> at step {esc(step)}'
    if info.from_branch:
        return f'variant of <code>{esc(info.from_branch)}</code>'
    return ""


def _revisions(info: BranchInfo) -> str:
    if len(info.history) <= 1:
        return ""
    items = "".join(
        f"<li><b>r{r.revision}</b> <span class='muted small'>{esc(r.saved)}</span>"
        f"{' · ' + esc(r.reason) if r.reason else ''}<ul class=plain>"
        + "".join(f"<li class='small'>{esc(c)}</li>" for c in r.changes) + "</ul></li>"
        for r in reversed(info.history)
    )
    return f'<div class="pop-section"><div class="pop-label">{len(info.history)} revisions</div><ul class="plain history">{items}</ul></div>'


def _shown_branches(project: ProjectSummary, snap: Snapshot, latest: dict[str, RunView]) -> list[str]:
    """Pinned first, then the most recently saved or run; archived ones only in Compare."""
    def info(name: str) -> BranchInfo:
        return snap.branches.get(f"{project.path}/{name}") or BranchInfo(project=project.path, name=name)

    def recency(name: str) -> str:
        run = latest.get(name)
        return max(info(name).saved.replace(" ", "T"), run.created_at if run else "")

    live = sorted(n for n in project.branches if not info(n).label.archived)
    live.sort(key=recency, reverse=True)  # stable: equal times keep the name order
    live.sort(key=lambda n: not info(n).label.pinned)
    return live[:RECENT_BRANCHES]


def _branch_menu(name: str, href: str, info: BranchInfo, run: RunView | None, snap: Snapshot) -> str:
    items = [f'<a href="#{esc(href)}">Open the pipeline</a>']
    if run is not None:
        # The branch as it is now: a finished run of an older version is not "done".
        stale = bool(info.keys) and bool(run.steps) and run.steps[-1].key not in info.keys
        older = f' <span class="muted small">{esc(run.state.lower())}, older version</span>' if stale else ""
        items.append(f'<a href="#runs/{esc(run.run_id)}">Latest run · r{esc(run.revision or "?")}{older}</a>')
        items.append(notebook_button(run, snap.notebooks, "Download notebook"))
    items.append(_revisions(info))
    return menu("".join(items), f"More about {name}")


def _branch_rows(project: ProjectSummary, snap: Snapshot, runs: tuple[RunView, ...], views: dict[str, str]) -> list[str]:
    latest: dict[str, RunView] = {}
    for run in runs:
        if run.project == project.path and run.branch and run.branch not in latest:
            latest[run.branch] = run
    everything = set(_datasets(project, snap))
    rows = []
    for name in _shown_branches(project, snap, latest):
        full = f"{project.path}/{name}"
        info = snap.branches.get(full) or BranchInfo(project=project.path, name=name)
        href = "pipelines/" + views.get(full, view_id(full))
        revision = f'<span class="tag">r{info.revision}</span>' if info.revision > 1 else ""
        # A branch's data is worth a word only when it is not simply the project's data.
        data = " + ".join(info.datasets) if info.datasets and set(info.datasets) != everything else ""
        origin = " · ".join(p for p in (_origin(info), esc(data)) if p)
        problem = f'<div class="note bad small">{esc(info.problem)}</div>' if info.problem else ""
        pinned = '<span class="star" title="pinned">★</span>' if info.label.pinned else ""
        rows.append(
            f'<div class="row-link" data-href="{esc(href)}"><div class="row-main">'
            f'<div class="row-title">{pinned}<a class="row-name" href="#{esc(href)}">{esc(name)}</a>{revision}'
            f'{f"<span class=origin>{origin}</span>" if origin else ""}</div>'
            f"{f'<div class=desc>{esc(info.description)}</div>' if info.description else ''}{problem}</div>"
            f'<div class="row-side">{pill(info.state, BRANCH_LABELS.get(info.state))}'
            f"{_branch_menu(name, href, info, latest.get(name), snap)}</div></div>"
        )
    return rows


def _datasets(project: ProjectSummary, snap: Snapshot) -> list[str]:
    used = {d for info in snap.branches.values() if info.project == project.path for d in info.datasets}
    return sorted(used | set(project.meta.datasets))


def _meta(project: ProjectSummary, snap: Snapshot) -> str:
    data = " + ".join(f'<a href="#library">{esc(n)}</a>' for n in _datasets(project, snap)) or "no data yet"
    count = len(project.branches)
    return f'<p class="meta">{data} · {count} branch{"es" if count != 1 else ""}</p>'


def _software(project: ProjectSummary, snap: Snapshot) -> str:
    env = snap.kernels.get(project.path)
    building = " · building…" if project.path in snap.env_builds else ""
    if env is None:
        return f'<div class="pop-section"><div class="pop-label">Software</div><div class="small">shared sc-hub environment{building}</div></div>'
    extra = ", ".join((*env.pip, *(f"{c} (conda)" for c in env.conda)))
    return (f'<div class="pop-section"><div class="pop-label">Software</div><div class="small">shared environment + '
            f'{esc(extra)}{building}</div><div class="muted small">Jupyter kernel "sc-hub: {esc(project.path)}" · '
            f"built {esc(env.built)}</div></div>")


def _project_menu(project: ProjectSummary, snap: Snapshot) -> str:
    log = "".join(f"<pre class=entry>{esc(e)}</pre>" for e in reversed(project.logbook_tail))
    logbook = f'<div class="pop-section"><div class="pop-label">Logbook, latest first</div>{log}</div>' if log else ""
    return menu(_software(project, snap) + logbook, f"More about {project.path}")


def _links(project: ProjectSummary, shown: int, project_views: dict[str, str]) -> str:
    total = len(project.branches)
    more = f'<span class="muted small">{shown} of {total}</span>' if total > shown else ""
    graph = (f'<a href="#pipelines/{esc(project_views[project.path])}">Map</a>' if project.path in project_views else "")
    return (f'<span class="links">{more}<a href="#experiments/project={esc(project.path)}" '
            f'title="Filter, sort and compare every branch">Compare all {total}</a>{graph}</span>')


def _project(project: ProjectSummary, snap: Snapshot, views: dict[str, str], project_views: dict[str, str]) -> str:
    rows = _branch_rows(project, snap, snap.runs, views)
    problems = "".join(f'<p class="note bad">{esc(p)}</p>' for p in project.problems)
    status = "" if project.meta.status == "active" else pill(PROJECT_STATE[project.meta.status], project.meta.status)
    return (
        f'<div class="project" data-project="{esc(project.path)}" hidden>'
        f'<div class="project-head"><h2>{esc(project.path)}</h2>{status}{_project_menu(project, snap)}</div>'
        f'<p class="question">{esc(project.meta.question or "No question written yet.")}</p>'
        + _meta(project, snap) + problems
        + f'<div class="section-head"><h3>Branches</h3>{hint(BRANCHES_HINT)}{_links(project, len(rows), project_views)}</div>'
        + (f'<div class="rows">{"".join(rows)}</div>' if rows
           else '<p class="empty">No branches yet. Ask your assistant to save one.</p>')
        + _ideas(project) + "</div>"
    )


def render_projects(snap: Snapshot, views: dict[str, str] | None = None, project_views: dict[str, str] | None = None) -> str:
    views, project_views = views or {}, project_views or {}
    if not snap.projects:
        return '<p class="empty">No projects yet. Ask your assistant to create one with your research question.</p>'
    tree = "".join(
        f'<button type="button" class="pipe" data-project-link="{esc(p.path)}" data-text="{esc(p.path.lower() + " " + p.meta.question.lower())}" '
        f'style="padding-left:{8 + 16 * p.path.count("/")}px"><span class="dot {esc(PROJECT_STATE[p.meta.status])}"></span>'
        f'{esc(p.path.rsplit("/", 1)[-1])}</button>'
        for p in snap.projects
    )
    search = '<input class="tree-filter" type="search" placeholder="Filter projects" aria-label="Filter projects">' if len(snap.projects) > 8 else ""
    cards = "".join(_project(p, snap, views, project_views) for p in snap.projects)
    return (f'<div class="master"><aside class="pipe-list project-tree">{search}{tree}</aside>'
            f'<div class="detail">{cards}</div></div>')
