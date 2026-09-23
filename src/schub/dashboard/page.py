"""The single-page shell: header, tabs, the six views, inline CSS and JS."""

from __future__ import annotations

from .collect import Snapshot
from .html import esc
from .script import SCRIPT
from .style import CSS
from .views_main import render_jobs, render_library, render_overview, render_projects
from .views_pipelines import render_pipelines
from .views_runs import ImageUrl, render_runs

TABS = (
    ("overview", "Overview"),
    ("pipelines", "Pipelines"),
    ("runs", "Runs"),
    ("jobs", "Jobs"),
    ("library", "Library"),
    ("projects", "Projects"),
)


def _counts(snap: Snapshot) -> dict[str, int]:
    return {
        "runs": len(snap.runs),
        "jobs": sum(j.name.startswith("schub-") for j in snap.jobs),
        "projects": len(snap.projects),
    }


def render_page(snap: Snapshot, image_url: ImageUrl) -> str:
    counts = _counts(snap)
    tabs = "".join(
        f'<a href="#{key}" data-tab="{key}">{esc(label)}'
        f'{f"<span class=count>{counts[key]}</span>" if key in counts else ""}</a>'
        for key, label in TABS
    )
    views = {
        "overview": render_overview(snap),
        "pipelines": render_pipelines(snap, image_url),
        "runs": render_runs(snap, image_url),
        "jobs": render_jobs(snap),
        "library": render_library(snap),
        "projects": render_projects(snap),
    }
    sections = "".join(f'<section class="view" data-view="{key}">{html}</section>' for key, html in views.items())
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>sc-hub · {esc(snap.user)}</title><style>{CSS}</style></head><body>"
        f'<header class="top"><div class="brand"><span class="logo"></span>sc-hub<span class="muted">· {esc(snap.user)}</span></div>'
        f'<nav class="tabs">{tabs}</nav><div class="meta"><span>Updated {esc(snap.generated_at)}</span>'
        '<button id="autorefresh" type="button" aria-pressed="true">Auto-refresh on</button></div></header>'
        f"<main>{sections}</main><script>{SCRIPT}</script></body></html>"
    )
