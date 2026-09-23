"""The single-page shell: the header (the sc-hub square is the account menu, then
the tabs, then a one-line status), the views, inline CSS and JS."""

from __future__ import annotations

from .collect import Snapshot
from .html import esc
from .notebooks import code_templates
from .script import SCRIPT
from .style import CSS
from .views_activity import status_chip
from .views_cluster import render_cluster
from .views_library import render_library
from .views_projects import render_projects
from ..state import Frozen
from .script_experiments import EXPERIMENTS_SCRIPT
from .views_experiments import render_experiments
from .views_pipelines import render_pipelines
from .views_runs import ImageUrl, render_runs

# Projects are the root: a question, its datasets and branches. Experiments list every
# branch to filter and compare, Pipelines show one graph at a time, Runs what ran and
# what runs now, Library what there is to use.
TABS = (
    ("projects", "Projects"),
    ("experiments", "Experiments"),
    ("pipelines", "Pipelines"),
    ("runs", "Runs"),
    ("library", "Library"),
)


def _counts(snap: Snapshot) -> dict[str, int]:
    return {"projects": len(snap.projects), "experiments": len(snap.branches), "runs": len(snap.runs)}


class Site(Frozen):
    index: str  # index.html
    files: dict[str, str]  # other text files of the view folder (br/<view>.js), by relative path


def _initials(user: str) -> str:
    return "".join(part[:1] for part in user.replace("_", ".").split(".")[:2]).upper() or "?"


def _iso(generated_at: str) -> str:
    """'2026-09-23 11:19 UTC' -> '2026-09-23T11:19:00Z' for the page's 'minutes ago'."""
    stamp = generated_at.removesuffix(" UTC").replace(" ", "T")
    return f"{stamp}:00Z" if len(stamp) == 16 else ""


def _account(snap: Snapshot) -> str:
    """The sc-hub square: who you are, the cluster overview, sessions, and page refresh."""
    where = f"login node {snap.overview.login_node}" if snap.overview and snap.overview.login_node else "the cluster"
    initials = esc(_initials(snap.user))
    return (
        f'<details class="account"><summary class="brand" title="{esc(snap.user)}: account, cluster and refresh">'
        f'<span class="logo">{initials}</span><b>sc-hub</b><span class="caret" aria-hidden="true"></span></summary>'
        f'<div class="menu"><div class="who"><span class="logo big">{initials}</span>'
        f'<div><b>{esc(snap.user)}</b><div class="muted small">on {esc(where)}</div></div></div>'
        '<a href="#cluster"><b>Cluster overview</b><span class="muted small">quotas, limits, your jobs, storage</span></a>'
        '<a href="#runs/sessions"><b>Interactive sessions</b><span class="muted small">JupyterLab and cellxgene</span></a>'
        '<hr><div class="menu-row"><span>Updated</span>'
        f'<span title="{esc(snap.generated_at)}"><b>{esc(snap.generated_at[11:])}</b> '
        f'<span class="muted small" data-ago="{esc(_iso(snap.generated_at))}"></span></span></div>'
        '<button id="autorefresh" type="button" role="switch" aria-checked="true" class="switch-row">'
        '<span>Auto-refresh every minute</span><span class="switch" aria-hidden="true"></span></button></div></details>'
        '<span class="paused" hidden>auto-refresh off</span>'
    )


def render_page(snap: Snapshot, image_url: ImageUrl) -> str:
    return render_site(snap, image_url).index


def render_site(snap: Snapshot, image_url: ImageUrl) -> Site:
    counts = _counts(snap)
    pipelines = render_pipelines(snap, image_url)
    tabs = "".join(
        f'<a href="#{key}" data-tab="{key}">{esc(label)}'
        f'{f"<span class=count>{counts[key]}</span>" if key in counts else ""}</a>'
        for key, label in TABS
    )
    views = {
        "projects": render_projects(snap, pipelines.views, pipelines.project_views),
        "experiments": render_experiments(snap, pipelines.views, image_url),
        "pipelines": pipelines.shell,
        "runs": render_runs(snap, image_url),
        "library": render_library(snap),
        "cluster": render_cluster(snap.overview),
    }
    sections = "".join(f'<section class="view" data-view="{key}">{html}</section>' for key, html in views.items())
    index = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>sc-hub · {esc(snap.user)}</title><style>{CSS}</style></head><body>"
        f'<header class="top">{_account(snap)}<nav class="tabs">{tabs}</nav>{status_chip(snap)}</header>'
        f"<main>{sections}</main>{code_templates(snap)}<script>{EXPERIMENTS_SCRIPT}</script><script>{SCRIPT}</script></body></html>"
    )
    return Site(index=index, files=pipelines.files)
