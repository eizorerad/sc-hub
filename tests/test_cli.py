from __future__ import annotations

import json

import pytest

from schub.cli import main
from schub.datasets import write_catalog_entry

from .conftest import library_datasets, make_adata


@pytest.fixture
def env(monkeypatch, settings, ctx, write_h5ad):
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.setenv("SCHUB_LIBRARY", str(settings.library))
    monkeypatch.setenv("PATH", "/nonexistent")  # no Slurm binaries: exercises error paths
    directory = library_datasets(settings) / "pbmc3k"
    write_catalog_entry(directory, {"title": "PBMC"})
    write_h5ad(make_adata(), directory=directory)
    return settings


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_discovery_commands(env, capsys):
    code, out, _ = run(capsys, "bricks")
    assert code == 0 and "qc_filter" in [b["name"] for b in json.loads(out)]
    assert json.loads(run(capsys, "brick", "qc_filter")[1])["version"] == "0.1.0"
    assert json.loads(run(capsys, "datasets")[1])[0]["name"] == "pbmc3k"
    assert json.loads(run(capsys, "inspect", "pbmc3k")[1])["state"]["n_obs"] == 60
    assert json.loads(run(capsys, "runs")[1]) == []


def test_plan_from_inline_json_and_file(env, capsys, tmp_path):
    code, out, _ = run(capsys, "plan", "pbmc3k", '[{"brick": "qc_filter"}]', "--species", "human")
    assert code == 0 and json.loads(out)["ok"] is True
    steps = tmp_path / "steps.json"
    steps.write_text('[{"brick": "annotate_celltypist"}]')
    summary = json.loads(run(capsys, "plan", "pbmc3k", str(steps))[1])
    assert summary["ok"] is False and summary["issues"][0]["code"] == "needs_lognorm"


def test_errors_exit_non_zero(env, capsys):
    plan_id = json.loads(run(capsys, "plan", "pbmc3k", '[{"brick": "qc_filter"}]')[1])["plan_id"]
    code, _, err = run(capsys, "submit", plan_id)
    assert code == 1 and "could not run squeue" in err
    code, _, err = run(capsys, "inspect", "missing")
    assert code == 1 and "fetch_asset" in err


def test_doctor_reports_without_raising(env, capsys):
    report = json.loads(run(capsys, "doctor")[1])
    assert report["datasets"] == ["pbmc3k (library)"]
    assert report["celltypist_models"] == ["Immune_All_Low.pkl"]
    assert str(report["partitions"]).startswith("FAILED")
    assert report["imports"]["anndata"] != ""


def test_project_commands(env, capsys, tmp_path):
    assert run(capsys, "project-new", "pbmc", "--question", "Which clusters?", "--dataset", "pbmc3k")[0] == 0
    spec = tmp_path / "main.yaml"
    spec.write_text("dataset: pbmc3k\nsteps:\n  - brick: qc_filter\n")
    code, out, _ = run(capsys, "branch-save", "pbmc", "main", str(spec))
    assert code == 0 and json.loads(out)["branch"] == "main"
    code, out, _ = run(capsys, "branch-save", "pbmc", "loose", '{"from": "main", "overrides": {"qc_filter": {"min_genes": 50}}}')
    assert json.loads(out)["steps"][0]["params"]["min_genes"] == 50
    assert json.loads(run(capsys, "branch-plan", "pbmc", "main")[1])["ok"]
    assert run(capsys, "idea-new", "pbmc", "qc", "--title", "Looser QC?")[0] == 0
    assert json.loads(run(capsys, "idea-set", "pbmc", "qc", "--status", "planned", "--branch", "loose")[1])["branches"] == ["loose"]
    assert run(capsys, "log", "pbmc", "Picked defaults.")[0] == 0
    projects = json.loads(run(capsys, "projects")[1])
    assert projects[0]["branches"] == ["loose", "main"]
    assert run(capsys, "assets", "pbmc3k", "kang2018", "--missing")[1].strip() == "kang2018"
    assert json.loads(run(capsys, "assets", "pbmc3k")[1])["pbmc3k"]["source"] == "library"
    assert json.loads(run(capsys, "dashboard")[1])["projects"] == 1
    code, _, err = run(capsys, "branch-plan", "ghost", "main")
    assert code == 1 and "does not exist" in err


def test_long_inline_json_is_not_mistaken_for_a_path():
    from schub.cli_projects import read_arg

    inline = '{"steps": [' + ",".join(['{"brick": "qc_filter", "params": {}}'] * 20) + "]}"
    assert len(inline) > 300 and read_arg(inline) == inline
