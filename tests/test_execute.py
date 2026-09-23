from __future__ import annotations

import json
import sys
import types
from dataclasses import replace

import pytest

from schub.bricks import REGISTRY, BrickError
from schub.execute import EXIT_CHECK_FAILED, EXIT_CRASH, main, run_step
from schub.state import DatasetState
from schub.stepfile import ERROR_FILE, STEP_FILE, SUCCESS, SUMMARY_FILE, StepFile


@pytest.fixture
def fake_impl(monkeypatch):
    module = types.ModuleType("fake_impl")
    behaviour = {"mode": "ok"}

    def run(io, params):
        if behaviour["mode"] == "check":
            raise BrickError("replicates missing")
        if behaviour["mode"] == "crash":
            raise ZeroDivisionError("oops")
        io.output.write_text("h5ad")
        return {"min_genes": params.min_genes, "input": str(io.input)}

    module.run = run
    monkeypatch.setitem(sys.modules, "fake_impl", module)
    registry = {"qc_filter": replace(REGISTRY["qc_filter"], impl="fake_impl:run")}
    return behaviour, registry


@pytest.fixture
def step_dir(tmp_path):
    directory = tmp_path / "step"
    directory.mkdir()
    step = StepFile(
        brick="qc_filter",
        version="0.1.0",
        params={"min_genes": 150},
        input=str(tmp_path / "in.h5ad"),
        output=str(directory / "output.h5ad"),
        results_dir=str(directory / "results"),
        state_in=DatasetState(n_obs=10, n_vars=5, x_kind="raw_counts"),
        context={},
    )
    (directory / STEP_FILE).write_text(step.model_dump_json())
    return directory


def test_success_writes_summary_and_marker(fake_impl, step_dir):
    _, registry = fake_impl
    assert run_step(step_dir, registry) == 0
    assert (step_dir / SUCCESS).exists()
    summary = json.loads((step_dir / "results" / SUMMARY_FILE).read_text())
    assert summary["min_genes"] == 150


def test_data_check_failure_is_recorded(fake_impl, step_dir):
    behaviour, registry = fake_impl
    behaviour["mode"] = "check"
    assert run_step(step_dir, registry) == EXIT_CHECK_FAILED
    error = json.loads((step_dir / ERROR_FILE).read_text())
    assert error == {"kind": "check_failed", "message": "replicates missing", "traceback": None}
    assert not (step_dir / SUCCESS).exists()


def test_crash_is_recorded_with_traceback(fake_impl, step_dir):
    behaviour, registry = fake_impl
    behaviour["mode"] = "crash"
    assert run_step(step_dir, registry) == EXIT_CRASH
    error = json.loads((step_dir / ERROR_FILE).read_text())
    assert error["kind"] == "crash" and "ZeroDivisionError" in error["traceback"]


def test_main_parses_arguments(monkeypatch, step_dir):
    seen = {}
    monkeypatch.setattr("schub.execute.run_step", lambda d: seen.setdefault("dir", d) and 0)
    main(["--step-dir", str(step_dir)])
    assert seen["dir"] == step_dir


def test_version_or_code_drift_refuses_to_run(fake_impl, step_dir):
    _, registry = fake_impl
    step = StepFile.model_validate_json((step_dir / STEP_FILE).read_text())
    (step_dir / STEP_FILE).write_text(step.model_copy(update={"version": "0.0.9"}).model_dump_json())
    assert run_step(step_dir, registry) == EXIT_CHECK_FAILED
    assert json.loads((step_dir / ERROR_FILE).read_text())["kind"] == "stale_plan"
    (step_dir / STEP_FILE).write_text(step.model_copy(update={"code_id": "old"}).model_dump_json())
    assert run_step(step_dir, registry) == EXIT_CHECK_FAILED
    assert "code changed" in json.loads((step_dir / ERROR_FILE).read_text())["message"]


def test_broken_step_file_leaves_a_record(fake_impl, step_dir):
    _, registry = fake_impl
    (step_dir / STEP_FILE).write_text("{not json")
    assert run_step(step_dir, registry) == EXIT_CRASH
    assert json.loads((step_dir / ERROR_FILE).read_text())["kind"] == "setup"
