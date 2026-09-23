from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from schub.brick_code import brick_source, code_changed, current_code_id
from schub.bricks import REGISTRY
from schub.notebook import NotebookRun, NotebookStep, load_run, render_notebook, write_notebook
from schub.notebook_kit import NotebookError, Pipeline
from schub.runs import RunManifest, StepRecord
from schub.stepfile import STEP_FILE, SUCCESS, SUMMARY_FILE

IMPL = Path(__file__).parents[1] / "src" / "schub" / "bricks" / "impl"


@pytest.mark.parametrize("brick", sorted(REGISTRY))
def test_every_brick_reads_as_one_notebook_cell(brick):
    source = brick_source(brick)
    tree = ast.parse(source)
    relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not relative and brick in names and "run" not in names


def test_brick_helpers_do_not_collide_in_one_notebook():
    """Every brick's code shares the notebook's namespace: only run() may repeat (it is renamed)."""
    seen: dict[str, str] = {}
    for path in sorted(IMPL.glob("*.py")):
        if path.name in {"__init__.py", "common.py"}:
            continue
        for node in ast.parse(path.read_text()).body:
            targets = [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else [
                t.id for t in getattr(node, "targets", [getattr(node, "target", None)]) if isinstance(t, ast.Name)]
            for name in targets:
                if name != "run":
                    assert name not in seen, f"{name} defined in {seen.get(name)} and {path.stem}"
                    seen[name] = path.stem


def test_code_changed_only_for_another_recorded_version():
    assert not code_changed("qc_filter", current_code_id("qc_filter"))
    assert code_changed("qc_filter", "0" * 16) and not code_changed("qc_filter", "")
    assert not code_changed("no_such_brick", "abc")


def _record(folder: Path, brick: str, source: Path, output: bool, params: dict, summary: dict | None = None) -> None:
    folder.mkdir(parents=True)
    state = {"n_obs": 60, "n_vars": 30, "x_kind": "raw_counts"}
    (folder / STEP_FILE).write_text(json.dumps({
        "brick": brick, "version": "1", "code_id": current_code_id(brick), "params": params, "input": str(source),
        "output": str(folder / "output.h5ad") if output else None, "results_dir": str(folder / "results"),
        "state_in": state, "context": {"library_roots": "/lib"}}))
    if summary is not None:
        (folder / "results").mkdir()
        (folder / "results" / SUMMARY_FILE).write_text(json.dumps(summary))
        (folder / "results" / "timing.json").write_text(json.dumps({"seconds": 400}))
        (folder / SUCCESS).write_text("ok")


@pytest.fixture
def recorded(tmp_path):
    steps = tmp_path / "steps"
    data = tmp_path / "data" / "kang" / "data.h5ad"
    data.parent.mkdir(parents=True)
    data.write_text("h5ad")
    _record(steps / "a", "qc_filter", data, True, {"min_genes": 300}, {"cells_final": 55})
    (steps / "a" / "output.h5ad").write_text("qc output")
    _record(steps / "b", "integrate_scvi", steps / "a" / "output.h5ad", True, {"batch_key": "donor"})
    _record(steps / "c", "pseudobulk_de", steps / "b" / "output.h5ad", False,
            {"condition_key": "label", "reference": "ctrl", "treatment": "stim", "replicate_key": "donor"})
    manifest = RunManifest(
        run_id="20260923-100000-abcdef-0000", plan_id="abcdef123456", created_at="2026-09-23T10:00:00",
        dataset=str(data), schub_version="0.4.0", project="ifn", branch="main", revision=2,
        steps=tuple(StepRecord(index=i, brick=b, step_key=k, step_dir=str(steps / k))
                    for i, (b, k) in enumerate((("qc_filter", "a"), ("integrate_scvi", "b"), ("pseudobulk_de", "c")), 1)),
    )
    return manifest


def test_notebook_has_each_step_with_its_code_and_parameters(recorded):
    run = load_run(recorded, question="How does IFN change each cell type?")
    assert run.dataset == "kang" and run.steps[0].completed and run.steps[0].headline == "55 cells kept"
    notebook = render_notebook(run)
    assert notebook["nbformat"] == 4 and len({c["id"] for c in notebook["cells"]}) == len(notebook["cells"])
    sources = ["".join(c["source"]) for c in notebook["cells"]]
    code = [s for c, s in zip(notebook["cells"], sources) if c["cell_type"] == "code"]
    for cell in code:
        ast.parse(cell)
    text = "\n".join(sources)
    assert "# ifn / main · r2" in text and "How does IFN change each cell type?" in text
    assert "def qc_filter(" in text and "def integrate_scvi(" in text and "def pseudobulk_de(" in text
    assert "    min_genes=300,\n" in text and "    max_pct_mt=" in text  # every parameter, defaults too
    assert "summary = qc_filter(io, params)" in text and "nb.io(2)" in text
    assert "`ifn/main#3`" in text and "needs a GPU" in text and "took 6m 40s" in text
    assert "no saved result" in text  # scVI never finished in this fake run
    assert "nb.load(2)" in text and "'de_all.csv'" in text and "'scvi_model'" in text
    assert "sc-hub was updated" not in text and "Recorded by an older sc-hub" not in text


def test_notebook_marks_code_that_changed_since_the_run(recorded):
    run = load_run(recorded)
    old = run.model_copy(update={"steps": (run.steps[0].model_copy(update={"code_id": "0" * 16}),)})
    assert "sc-hub was updated after this run" in json.dumps(render_notebook(old))


def test_pipeline_reads_the_saved_result_until_the_step_is_re_run_here(recorded, tmp_path):
    steps = [(s.index, s.brick, s.step_dir) for s in recorded.steps]
    nb = Pipeline(steps, work=tmp_path / "work")
    first = nb.io(1)
    assert first.input == Path(recorded.dataset) and first.output == tmp_path / "work" / "1_qc_filter" / "output.h5ad"
    assert first.state_in.n_obs == 60 and first.context == {"library_roots": "/lib"} and first.results_dir.is_dir()
    assert nb.io(2).input == Path(recorded.steps[0].step_dir) / "output.h5ad"  # the pipeline's saved result
    first.output.write_text("mine")
    assert nb.io(2).input == first.output  # re-run here: the next step uses it
    with pytest.raises(NotebookError, match="run step 2 above first"):
        nb.io(3)  # scVI never finished and was not re-run here
    assert nb.io(1).output is not None and nb.file(1, "summary.json") == Path(recorded.steps[0].step_dir) / "results" / "summary.json"
    nb.reset(1)
    assert not first.output.exists() and nb.io(2).input == Path(recorded.steps[0].step_dir) / "output.h5ad"
    with pytest.raises(NotebookError, match="no step 9"):
        nb.io(9)


def test_terminal_step_writes_no_h5ad(recorded, tmp_path):
    nb = Pipeline([(s.index, s.brick, s.step_dir) for s in recorded.steps], work=tmp_path / "work")
    nb.mine(2).parent.mkdir(parents=True)
    nb.mine(2).write_text("mine")
    assert nb.io(3).output is None


def test_notebook_refreshes_until_the_student_edits_it(tmp_path):
    step = NotebookStep(index=1, brick="qc_filter", step_dir="/l/steps/a")
    run = NotebookRun(run_id="20260923-100000-abcdef-0000", dataset="kang", created_at="now", steps=(step,))
    path = write_notebook(tmp_path, run)
    assert "no saved result" in path.read_text()
    done = run.model_copy(update={"steps": (step.model_copy(update={"completed": True, "headline": "55 cells kept"}),)})
    write_notebook(tmp_path, done)
    assert "55 cells kept" in path.read_text()  # untouched notebook: refreshed
    edited = json.loads(path.read_text())
    edited["cells"].append({"cell_type": "code", "source": ["my analysis"], "metadata": {}, "outputs": [], "execution_count": None})
    path.write_text(json.dumps(edited))
    write_notebook(tmp_path, run)
    assert "my analysis" in path.read_text()  # edited: kept


def test_old_parameters_helpers_and_repeated_bricks_are_explained():
    steps = (
        NotebookStep(index=1, brick="pseudobulk_de", step_dir="/s/a", params={"condition_key": "label"}),
        NotebookStep(index=2, brick="memento_de", step_dir="/s/b"),
        NotebookStep(index=3, brick="pseudobulk_de", step_dir="/s/c"),
    )
    text = json.dumps(render_notebook(NotebookRun(run_id="r", dataset="d", created_at="now", steps=steps)))
    assert "Recorded by an older sc-hub" in text  # invalid recorded params are shown, with a note
    cells = ["".join(c["source"]) for c in render_notebook(NotebookRun(run_id="r", dataset="d", created_at="now", steps=steps))["cells"]]
    memento = next(c for c in cells if "def memento_de(" in c)
    guard = "\n".join(line for line in memento.splitlines() if "_write_csv" in line and "import" in line or "globals()" in line)
    namespace: dict = {"_write_csv": "edited"}
    exec(guard, namespace)  # noqa: S102
    assert namespace["_write_csv"] == "edited"  # the pseudobulk cell ran: its (edited) helper stays
    namespace = {}
    exec(guard, namespace)  # noqa: S102
    assert callable(namespace["_write_csv"])  # started at memento: the library's helper
    assert text.count("def pseudobulk_de(") == 1 and "in step 1's section above" in text


def test_relative_work_folder_is_made_absolute(recorded, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nb = Pipeline([(s.index, s.brick, s.step_dir) for s in recorded.steps], work="work/x")
    io = nb.io(1)
    assert io.output.is_absolute() and io.results_dir == tmp_path / "work" / "x" / "1_qc_filter" / "results"


def test_a_notebook_the_student_ran_is_kept(tmp_path):
    step = NotebookStep(index=1, brick="qc_filter", step_dir="/l/steps/a")
    run = NotebookRun(run_id="20260923-100000-abcdef-0000", dataset="kang", created_at="now", steps=(step,))
    path = write_notebook(tmp_path, run)
    ran = json.loads(path.read_text())
    ran["cells"][1]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": ["done"]}]
    ran["cells"][1]["execution_count"] = 1
    path.write_text(json.dumps(ran))
    write_notebook(tmp_path, run.model_copy(update={"question": "new"}))
    assert '"done"' in path.read_text()  # outputs survive
