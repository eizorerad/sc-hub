"""The single-page shell: the header (the sc-hub square is the menu with everything
besides research: runs, library, sessions, cluster; then the Journal tab, then a
one-line status), the views, inline CSS and JS."""

from __future__ import annotations

from .collect import Snapshot
from .html import esc
from .notebooks import code_templates
from .script import SCRIPT
from .style import CSS, JOURNAL_CSS
from .views_activity import status_chip
from .views_cluster import render_cluster
from .views_library import render_library
from ..state import Frozen
from .views_journal import JOURNAL_SCRIPT, render_journal
from .views_runs import ImageUrl, render_runs

# What a researcher needs is the Journal. Runs of brick pipelines, the Library, sessions
# and the cluster are one click away in the menu under the sc-hub square.
MENU_VIEWS = {"runs": "Runs", "library": "Library"}  # the cluster overview has its own title


class Site(Frozen):
    index: str  # index.html


def _initials(user: str) -> str:
    return "".join(part[:1] for part in user.replace("_", ".").split(".")[:2]).upper() or "?"


def _iso(generated_at: str) -> str:
    """'2026-09-23 11:19 UTC' -> '2026-09-23T11:19:00Z' for the page's 'minutes ago'."""
    stamp = generated_at.removesuffix(" UTC").replace(" ", "T")
    return f"{stamp}:00Z" if len(stamp) == 16 else ""


def _account(snap: Snapshot) -> str:
    """The sc-hub square: who you are, runs, library, sessions, the cluster, and page refresh."""
    where = f"login node {snap.overview.login_node}" if snap.overview and snap.overview.login_node else "the cluster"
    initials = esc(_initials(snap.user))
    return (
        f'<details class="account"><summary class="brand" title="{esc(snap.user)}: runs, library, cluster and refresh">'
        f'<span class="logo">{initials}</span><b>sc-hub</b><span class="caret" aria-hidden="true"></span></summary>'
        f'<div class="menu"><div class="who"><span class="logo big">{initials}</span>'
        f'<div><b>{esc(snap.user)}</b><div class="muted small">on {esc(where)}</div></div></div>'
        '<a href="#runs"><b>Runs</b><span class="muted small">history, queue, what runs now</span></a>'
        '<a href="#library"><b>Library</b><span class="muted small">datasets, models, tools</span></a>'
        '<a href="#runs/sessions"><b>Interactive sessions</b><span class="muted small">JupyterLab and cellxgene</span></a>'
        '<a href="#cluster"><b>Cluster overview</b><span class="muted small">quotas, limits, your jobs, storage</span></a>'
        '<hr><div class="menu-row"><span>Updated</span>'
        f'<span title="{esc(snap.generated_at)}"><b>{esc(snap.generated_at[11:])}</b> '
        f'<span class="muted small" data-ago="{esc(_iso(snap.generated_at))}"></span></span></div>'
        '<button id="autorefresh" type="button" role="switch" aria-checked="true" class="switch-row">'
        '<span>Auto-refresh every minute</span><span class="switch" aria-hidden="true"></span></button></div></details>'
        '<span class="paused" hidden>auto-refresh off</span>'
    )


def render_page(snap: Snapshot, image_url: ImageUrl) -> str:
    return render_site(snap, image_url).index


def _titled(key: str, html: str) -> str:
    """Views opened from the menu get a title, so it is clear where you are."""
    title = MENU_VIEWS.get(key)
    return f'<h1 class="view-title">{esc(title)}</h1>{html}' if title else html


def render_site(snap: Snapshot, image_url: ImageUrl) -> Site:
    tabs = '<a href="#journal" data-tab="journal">Journal</a>'
    views = {"journal": render_journal(snap.journals, snap.bench)}
    views.update({
        "runs": render_runs(snap, image_url),
        "library": render_library(snap),
        "cluster": render_cluster(snap.overview),
    })
    sections = "".join(f'<section class="view" data-view="{key}">{_titled(key, html)}</section>' for key, html in views.items())
    index = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>sc-hub · {esc(snap.user)}</title><style>{CSS}{JOURNAL_CSS}</style></head><body>"
        f'<header class="top">{_account(snap)}<nav class="tabs">{tabs}</nav>{status_chip(snap)}</header>'
        f"<main>{sections}</main>{code_templates(snap)}"
        f"<script>{JOURNAL_SCRIPT}</script><script>{SCRIPT}</script></body></html>"
    )
    return Site(index=index)
