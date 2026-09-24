from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from schub.notebook import NotebookRun, NotebookStep, render_notebook, write_notebook
from schub.notebook_results import MAX_FIGURE_BYTES, Budget, anndata_summary, csv_head, step_outputs, warning_lines

from .conftest import make_adata

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
RUN_ID = "20260923-100000-abcdef-0000"


def _step(folder: Path, summary: dict, figures: dict[str, bytes] | None = None) -> Path:
    results = folder / "results"
    results.mkdir(parents=True)
    (results / "summary.json").write_text(json.dumps(summary))
    for name, data in (figures or {}).items():
        (results / name).write_bytes(data)
    (folder / "_SUCCESS").write_text("ok")
    return folder


def test_step_outputs_show_numbers_warnings_and_figures(tmp_path):
    folder = _step(tmp_path / "a", {"cells_final": 1098, "median_pct_mt": 10.2934, "warnings": ["no MT- genes"],
                                    "groups": {"x": 1}, "figure": "/l/steps/a/results/qc.png"},
                   {"qc.png": PNG, "qc_thumb.png": PNG, "huge.png": b"0" * (MAX_FIGURE_BYTES + 1)})
    problems: list[str] = []
    outputs = step_outputs(str(folder), Budget(), problems)
    kinds = [o["output_type"] for o in outputs]
    assert kinds == ["stream", "display_data", "stream", "stream", "display_data"]  # note, table, warning, too big, png
    html = "".join(outputs[1]["data"]["text/html"])
    assert "cells_final" in html and "1098" in html and "10.29" in html and "groups" not in html and "qc.png" not in html
    assert outputs[2]["name"] == "stderr" and "no MT- genes" in "".join(outputs[2]["text"])
    assert "huge.png, 2.0 MB, left out: too large" in "".join(outputs[3]["text"])
    assert base64.b64decode(outputs[4]["data"]["image/png"]) == PNG  # the thumbnail is left out
    assert step_outputs(str(tmp_path / "missing"), Budget(), problems) == [] and problems == []


def test_figures_stop_at_the_notebook_budget(tmp_path):
    folder = _step(tmp_path / "a", {"n": 1}, {"a.png": PNG, "b.png": PNG})
    outputs = step_outputs(str(folder), Budget(total=len(PNG)), [])
    assert sum("image/png" in o.get("data", {}) for o in outputs) == 1
    assert "already holds enough figures" in json.dumps(outputs)


@pytest.mark.parametrize("raw,expected", [(["a", 3], ["a"]), ("one", ["one"]), (3, []), (None, [])])
def test_warnings_of_any_shape(raw, expected):
    assert warning_lines({"warnings": raw}) == expected


def test_tables_are_escaped_cut_and_tolerate_other_encodings(tmp_path):
    path = tmp_path / "de_all.csv"
    path.write_text("gene,group,padj\n" + "".join(f"<b>G{i}</b>,T,0.{i}\n" for i in range(40)))
    (out,) = csv_head(path, [])
    html = "".join(out["data"]["text/html"])
    assert "&lt;b&gt;G0&lt;/b&gt;" in html and "<b>G0" not in html and html.count("<tr>") == 31  # header + 30
    latin = tmp_path / "latin.csv"
    latin.write_bytes("gene,note\nIFI6,caf\xe9\n".encode("latin-1"))
    assert "IFI6" in json.dumps(csv_head(latin, []))
    assert csv_head(tmp_path / "none.csv", []) == []


def test_anndata_summary_reads_the_index_and_reports_broken_files(tmp_path):
    adata = make_adata(n_obs=40)
    adata.obsm["X_umap"] = adata.X[:, :2].toarray() if hasattr(adata.X, "toarray") else adata.X[:, :2]
    adata.layers["counts"] = adata.X.copy()
    path = tmp_path / "output.h5ad"
    adata.write_h5ad(path)
    problems: list[str] = []
    (out,) = anndata_summary(path, problems)
    text = "".join(out["data"]["text/plain"])
    assert text.startswith(f"AnnData object with n_obs × n_vars = 40 × {adata.n_vars}")
    assert "obsm: 'X_umap'" in text and "layers: 'counts'" in text and problems == []
    assert anndata_summary(tmp_path / "missing.h5ad", problems) == [] and problems == []  # absent: fine
    broken = tmp_path / "broken.h5ad"
    broken.write_text("not hdf5")
    assert anndata_summary(broken, problems) == [] and len(problems) == 1  # there but unreadable: noted


def _run(tmp_path: Path) -> NotebookRun:
    qc = _step(tmp_path / "qc", {"cells_final": 55}, {"qc_distributions.png": PNG})
    make_adata(n_obs=55).write_h5ad(qc / "output.h5ad")
    de = _step(tmp_path / "de", {"groups": {}})
    (de / "results" / "de_all.csv").write_text("gene,padj\nIFI6,1e-9\n")
    steps = (
        NotebookStep(index=1, brick="qc_filter", step_dir=str(qc), completed=True),
        NotebookStep(index=2, brick="pseudobulk_de", step_dir=str(de), completed=True),
    )
    return NotebookRun(run_id=RUN_ID, dataset="kang", created_at="now", steps=steps)


def test_downloaded_notebook_carries_the_saved_results(tmp_path):
    run = _run(tmp_path)
    notebook = render_notebook(run)
    with_outputs = [c for c in notebook["cells"] if c.get("outputs")]
    assert all(c["execution_count"] is None for c in with_outputs)  # shown, not run
    text = json.dumps(with_outputs)
    assert "cells_final" in text and "image/png" in text and "IFI6" in text and "AnnData object" in text
    assert "Results are inside" in "".join(notebook["cells"][0]["source"]) and "schub_incomplete" not in notebook["metadata"]
    bare = render_notebook(run, results=False)
    assert not any(c.get("outputs") for c in bare["cells"]) and "No results inside" in "".join(bare["cells"][0]["source"])
    unfinished = run.model_copy(update={"steps": tuple(s.model_copy(update={"completed": False}) for s in run.steps)})
    assert "No results inside" in "".join(render_notebook(unfinished)["cells"][0]["source"])


def test_unreadable_saved_files_are_listed(tmp_path):
    run = _run(tmp_path)
    (Path(run.steps[0].step_dir) / "results" / "summary.json").write_text("{broken")
    (problem,) = render_notebook(run)["metadata"]["schub_incomplete"]
    assert problem.startswith(str(Path(run.steps[0].step_dir) / "results" / "summary.json"))


def test_a_notebook_with_saved_outputs_still_refreshes_until_it_is_run(tmp_path):
    folder = _step(tmp_path / "qc", {"cells_final": 55})
    step = NotebookStep(index=1, brick="qc_filter", step_dir=str(folder), completed=True)
    run = NotebookRun(run_id=RUN_ID, dataset="kang", created_at="now", steps=(step,))
    path = write_notebook(tmp_path / "nb", run)
    write_notebook(tmp_path / "nb", run.model_copy(update={"question": "new question"}))
    assert "new question" in path.read_text()  # outputs alone do not make it the student's
    ran = json.loads(path.read_text())
    ran["cells"][1]["execution_count"] = 1
    path.write_text(json.dumps(ran))
    write_notebook(tmp_path / "nb", run.model_copy(update={"question": "newer"}))
    assert "newer" not in path.read_text()  # run by the student: kept


def _snapshot(runs, projects=()):
    from schub.dashboard.collect import Snapshot

    return Snapshot(generated_at="t", user="u", library_mode="m", env_id="e", versions={}, jobs=(), runs=tuple(runs),
                    projects=tuple(projects), datasets=(), models=(), nodes=())


def _view_run(run_id: str, folder: Path):
    from schub.dashboard.collect import RunView, StepView

    step = StepView(index=1, brick="qc_filter", key="k", state="COMPLETED", step_dir=str(folder),
                    summary={"cells_final": 55}, headline="55 cells kept")
    return RunView(run_id=run_id, project="ifn", branch="main", created_at="now", dataset="kang",
                   state="COMPLETED", steps=(step,))


def test_dashboard_notebooks_always_have_results_and_rerender_only_on_change(tmp_path, monkeypatch):
    from schub.dashboard import _write
    from schub.dashboard import notebooks as nbs

    folder = _step(tmp_path / "qc", {"cells_final": 55}, {"qc_distributions.png": PNG})
    newest = _view_run("20260923-100000-abcdef-0001", folder)
    older = _view_run("20260923-090000-abcdef-0002", folder)  # same branch, superseded
    other = _view_run("20260923-080000-abcdef-0003", folder).model_copy(update={"branch": "alt"})  # its branch's latest
    monkeypatch.setattr(nbs, "RECENT_FIGURES", 1)
    view = tmp_path / "view"
    runs = [newest, older, other]
    assert nbs.write_notebooks(view, _snapshot(runs), _write) == {r.run_id for r in runs}
    text = {r.run_id: (view / "nb" / f"{r.run_id}.js").read_text() for r in runs}
    assert text[newest.run_id].startswith(nbs.KEY_PREFIX) and "image/png" in text[newest.run_id]
    assert "cells_final" in text[older.run_id] and "image/png" not in text[older.run_id]  # results, no figures
    assert "not in this copy of an older run" in text[older.run_id]
    assert "image/png" in text[other.run_id]  # the latest run of its branch keeps its figures

    real = nbs.render_notebook
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(nbs, "render_notebook", counting)
    nbs.write_notebooks(view, _snapshot(runs), _write)
    assert calls == []  # nothing changed: nothing rendered, no figures read
    runs[2] = other.model_copy(update={"branch": "alt2"})
    nbs.write_notebooks(view, _snapshot(runs), _write)
    assert len(calls) == 1  # one run changed: only its notebook is rendered again
    monkeypatch.setattr(nbs, "RECENT_FIGURES", 2)
    nbs.write_notebooks(view, _snapshot(runs), _write)
    assert len(calls) == 2 and "image/png" in (view / "nb" / f"{older.run_id}.js").read_text()  # joined the window
    monkeypatch.setattr(nbs, "module_stamp", lambda module: (module, 42))  # sc-hub updated
    nbs.write_notebooks(view, _snapshot(runs), _write)
    assert len(calls) == 5


def test_incomplete_notebooks_are_rendered_again_and_failures_keep_the_last_one(tmp_path, monkeypatch):
    from schub.dashboard import _write
    from schub.dashboard import notebooks as nbs

    folder = _step(tmp_path / "qc", {"cells_final": 55})
    (folder / "results" / "summary.json").write_text("{broken")  # a read that failed
    run = _view_run("20260923-100000-abcdef-0001", folder)
    view = tmp_path / "view"
    nbs.write_notebooks(view, _snapshot([run]), _write)
    script = view / "nb" / f"{run.run_id}.js"
    assert script.read_text().splitlines()[0].endswith("-incomplete")
    (folder / "results" / "summary.json").write_text(json.dumps({"cells_final": 55}))  # readable again
    nbs.write_notebooks(view, _snapshot([run]), _write)
    assert not script.read_text().splitlines()[0].endswith("-incomplete") and "cells_final" in script.read_text()

    def boom(*args, **kwargs):
        raise TypeError("odd run")

    monkeypatch.setattr(nbs, "render_notebook", boom)
    assert nbs.write_notebooks(view, _snapshot([run.model_copy(update={"branch": "x"})]), _write) == {run.run_id}
    assert script.exists()  # the build goes on and the last good notebook stays downloadable
