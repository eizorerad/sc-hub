"""The dashboard shows what a researcher needs and folds the rest away.

Found on the live pilot: the page itself needed learning. Every block carried a
paragraph of explanation, a step used by four branches repeated the same note and
the same 'ask your assistant' box four times, and parameters, code and job details
stood between the student and the step's result."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from schub.dashboard.collect import collect
from schub.dashboard.html import hint, hint_html, menu
from schub.datasets import write_catalog_entry
from schub.service import Hub
from schub.slurm import Slurm

from .conftest import library_datasets, make_adata
from .test_dashboard import MAIN, finish


@pytest.fixture
def hub(settings, cluster, ctx, write_h5ad):
    directory = library_datasets(settings) / "kang2018"
    write_catalog_entry(directory, {"title": "Kang"})
    adata = make_adata(n_obs=90)
    adata.obs["cell_type"] = pd.Categorical(["T", "B", "Mono"][i % 3] for i in range(adata.n_obs))
    write_h5ad(adata, directory=directory)
    hub = Hub(settings, Slurm(cluster))
    hub.create_project("ifn", question="IFN response by cell type")
    return hub


@pytest.fixture
def finished_main(hub, cluster):
    plan = hub.save_branch("ifn", "main", MAIN)
    run = hub.submit(plan.plan_id)
    for step, summary in zip(run.steps, ({"cells_final": 88, "warnings": ["no MT- genes: filter had no effect"]},
                                         {"n_clusters": 9})):
        finish(Path(step.step_dir), summary)
        cluster.jobs[step.job_id] = "COMPLETED"
    return run


def test_hints_and_menus_keep_text_out_of_the_page_and_escape_it():
    tip = hint('<img src=x onerror="alert(1)"> & more')
    assert "<img" not in tip and "&lt;img" in tip and 'role="tooltip"' in tip and 'class="hint"' in tip
    assert 'class="hint end"' in hint("x", end=True)
    assert '<span class="legend">' in hint_html('<span class="legend"></span>', "What the colours mean")
    box = menu("<a href='#x'>Open</a>", "More about main")
    assert box.startswith('<details class="menu-pop end">') and 'aria-label="More about main"' in box
    assert "<a href='#x'>Open</a>" in box and 'class="menu-pop"' in menu("", end=False)


def test_a_request_about_an_older_version_says_so_in_plain_sight():
    from schub.dashboard.html import ASK_HINT, ask_block

    box = ask_block("ifn/main#2", "normalize_embed", "main", "This run used an earlier version of the branch.")
    head, body = box.split('<span class="tip" role="tooltip">', 1)[1].split("</span>", 1)
    assert "earlier version" not in head and '<p class="note warn small">This run used an earlier version' in body
    assert "revision" in ASK_HINT and '<code class="ref">ifn/main#2</code>' in box and "ask-ref" not in box


def test_a_run_row_names_the_run_and_keeps_its_id_in_the_tooltip(hub, finished_main):
    from schub.dashboard.views_runs import render_runs

    run_id = finished_main.run_id
    html = render_runs(collect(hub), lambda *_: None)
    row = html.split(f'data-href="runs/{run_id}"', 1)[1].split("</tr>", 1)[0]
    assert f'title="run {run_id}">ifn / main' in row and f'<div class="muted small">{run_id}' not in row
    assert run_id in html.split(f'data-href="runs/{run_id}"', 1)[1].split(">", 1)[0]  # still found by the search box
