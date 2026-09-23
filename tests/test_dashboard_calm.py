"""The dashboard shows what a researcher needs and folds the rest away.

Found on the live pilot: the page itself needed learning. Every block carried a
paragraph of explanation, a step used by four branches repeated the same note and
the same 'ask your assistant' box four times, and parameters, code and job details
stood between the student and the step's result."""

from __future__ import annotations

from pathlib import Path

from schub.dashboard.collect import collect
from schub.dashboard.html import hint, hint_html, menu
from schub.projects import BranchSpec

from .dashboard_helpers import templates_of
from .test_dashboard import MAIN, finish
from .test_dashboard_history import finished_main, hub, pipelines, sdk_update  # noqa: F401 - fixtures


def test_hints_and_menus_keep_text_out_of_the_page_and_escape_it():
    tip = hint('<img src=x onerror="alert(1)"> & more')
    assert "<img" not in tip and "&lt;img" in tip and 'role="tooltip"' in tip and 'class="hint"' in tip
    assert 'class="hint end"' in hint("x", end=True)
    assert '<span class="legend">' in hint_html('<span class="legend"></span>', "What the colours mean")
    box = menu("<a href='#x'>Open</a>", "More about main")
    assert box.startswith('<details class="menu-pop end">') and 'aria-label="More about main"' in box
    assert "<a href='#x'>Open</a>" in box and 'class="menu-pop"' in menu("", end=False)


def _template(files: dict[str, str], view: str, key: str) -> str:
    return templates_of(files, view).split(f'<template data-node="{key}">', 1)[1].split("</template>", 1)[0]


def test_a_step_shared_by_branches_has_one_request_box_and_its_result_first(hub, finished_main):
    hub.save_branch("ifn", "twin", MAIN)  # the same steps: every step is shared
    snap = collect(hub)
    qc = finished_main.steps[0].step_key
    panel = _template(pipelines(snap).files, "v-ifn-main", qc)
    assert panel.count('data-ask="fix"') == 1 and panel.count('data-ask="fork"') == 1
    assert '<select class="ask-ref" aria-label="Which branch"><option value="">which branch?</option>' in panel  # no silent default
    assert '<option value="ifn/main#1">ifn/main</option>' in panel and '<option value="ifn/twin#1">ifn/twin</option>' in panel
    # what it gave, then the request, then parameters / code / log / details folded
    assert panel.index("88 cells kept") < panel.index('<div class="ask"') < panel.index('<div class="folds">')
    folds = panel.split('<div class="folds">', 1)[1]
    assert '<details class="fold"><summary>Parameters</summary>' in folds and "Step key" in folds
    assert "Used by" not in panel.split('<div class="folds">', 1)[0]


def test_a_step_to_run_again_says_so_once_for_every_branch_with_the_same_reason(hub, finished_main, cluster, monkeypatch):
    hub.save_branch("ifn", "res-2", BranchSpec(from_branch="main", overrides={"normalize_embed": {"leiden_resolution": 2.0}}))
    run = hub.submit(hub.plan_branch("ifn", "res-2").plan_id)  # QC is main's QC; clustering is its own
    for step in run.steps[1:]:
        finish(Path(step.step_dir), {"n_clusters": 12})
        cluster.jobs[step.job_id] = "COMPLETED"
    sdk_update(monkeypatch)
    snap = collect(hub)
    new_qc = snap.branches["ifn/main"].keys[0]
    assert set(next(n for n in snap.nodes if n.key == new_qc).earlier) == {"ifn/main", "ifn/res-2"}
    panel = _template(pipelines(snap).files, "v-ifn-main", new_qc)
    assert panel.count("Needs a re-run") == 1
    assert "Needs a re-run in ifn/main, ifn/res-2: sc-hub or a step before it changed" in panel
    assert "Before, it gave <b>88 cells kept</b>" in panel


def test_a_project_page_is_the_question_and_its_branches(hub, finished_main):
    from schub.dashboard.views_projects import render_projects

    hub.fork_branch("ifn", "main", 2, "coarse", "fewer clusters", params={"leiden_resolution": 0.4})
    page = render_projects(collect(hub))
    assert '<p class="question">IFN response by cell type</p>' in page
    assert 'class="row-link" data-href="pipelines/v-ifn-main"' in page  # a whole row opens the pipeline
    row = page.split('data-href="pipelines/v-ifn-main"', 1)[1].split('class="row-link"', 1)[0]
    assert row.index('<details class="menu-pop end">') < row.index("Latest run · r1")  # the run: in the ⋯
    assert "fork</span> of <code>main@r1</code> at step 2" in page
    assert "No ideas yet" not in page and "Subprojects" not in page  # nothing to say, nothing shown
    assert "active</span>" not in page  # the usual state is not worth a label


def test_rerun_notes_merge_only_the_same_earlier_result(hub, finished_main, monkeypatch):
    from schub.dashboard.step_panel import _notes

    sdk_update(monkeypatch)
    snap = collect(hub)
    nodes = {n.key: n for n in snap.nodes}
    old_qc = finished_main.steps[0].step_key
    twin = nodes[old_qc].model_copy(update={"key": "an-other-qc"})  # same headline, another result
    new_qc = nodes[snap.branches["ifn/main"].keys[0]].model_copy(update={
        "labels": ("ifn/a", "ifn/b", "ifn/main"), "earlier": {"ifn/a": old_qc, "ifn/main": old_qc, "ifn/b": "an-other-qc"}})
    notes = _notes(new_qc, snap, {**nodes, "an-other-qc": twin})
    assert notes.count("Needs a re-run") == 2 and "in ifn/a, ifn/main:" in notes and "in ifn/b:" in notes
    assert f'data-select-node="{old_qc}"' in notes and 'data-select-node="an-other-qc"' in notes


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
