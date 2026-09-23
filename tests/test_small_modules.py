from __future__ import annotations

import ast
import json

import numpy as np
import pytest
from scipy import sparse

from schub.aggregate import pseudobulk
from schub.audit import audited
from schub.config import load_settings
from schub.notebook import render_notebook
from schub.runs import RunManifest, RunResults, StepResults


def test_pseudobulk_sums_counts_per_label():
    counts = sparse.csr_matrix(np.array([[1, 0], [2, 1], [0, 5]], dtype=np.float32))
    samples, summed, n_cells = pseudobulk(counts, ["b", "a", "b"])
    assert samples == ["a", "b"]
    np.testing.assert_array_equal(summed, [[2, 1], [1, 5]])
    np.testing.assert_array_equal(n_cells, [1, 2])
    _, dense_sum, _ = pseudobulk(np.array([[1.0], [3.0]]), ["x", "x"])
    np.testing.assert_array_equal(dense_sum, [[4.0]])
    with pytest.raises(ValueError):
        pseudobulk(counts, ["a"])


def test_load_settings_from_env(tmp_path):
    s = load_settings({"SCHUB_ROOT": str(tmp_path), "SCHUB_MAX_ACTIVE_RUNS": "5", "SCHUB_EXTRA_ROOTS": "/a:/b"})
    assert s.root == tmp_path and s.library is None and s.library_roots == (tmp_path / "library-local",)
    assert s.limits.max_active_runs == 5
    assert s.extra_roots[0].as_posix() == "/a"
    assert s.celltypist_home == tmp_path / "library-local" / "models" / "celltypist"
    with pytest.raises(ValueError, match="SCHUB_MAX_GPU_HOURS"):
        load_settings({"SCHUB_ROOT": str(tmp_path), "SCHUB_MAX_GPU_HOURS": "lots"})
    fallback = load_settings({"USER": ""})
    assert fallback.root.name == "schub"


def test_audit_records_success_and_failure(tmp_path):
    with audited(tmp_path, "tool_a", {"big": "x" * 1000}):
        pass
    with pytest.raises(RuntimeError), audited(tmp_path, "tool_b", {}):
        raise RuntimeError("nope")
    records = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    assert records[0]["ok"] and records[0]["args"]["big"].endswith("...")
    assert records[1] == {**records[1], "ok": False, "tool": "tool_b"}


def test_notebook_is_valid_jupyter_with_valid_code_cells():
    manifest = RunManifest(
        run_id="20260923-100000-abcdef", plan_id="abcdef123456", created_at="now",
        dataset="/l/data.h5ad", steps=(), schub_version="0.1.0",
    )
    results = RunResults(
        run_id=manifest.run_id, state="COMPLETED", final_output="/l/out.h5ad",
        steps=(StepResults(index=1, brick="pseudobulk_de", summary={"a|b": 1}, files=("/l/r/de_all.csv",)),),
    )
    notebook = render_notebook(manifest, results)
    assert notebook["nbformat"] == 4 and notebook["cells"][0]["cell_type"] == "markdown"
    code = ["".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"]
    for source in code:
        ast.parse(source)
    joined = "\n".join(code)
    assert "'/l/out.h5ad'" in joined and "'/l/r/de_all.csv'" in joined and "scvi" not in joined
    with_model = results.model_copy(update={"steps": results.steps + (
        StepResults(index=2, brick="integrate_scvi", summary={}, files=("/l/r/scvi_model/model.pt",)),)})
    assert "'/l/r/scvi_model'" in "".join("".join(c["source"]) for c in render_notebook(manifest, with_model)["cells"])


def test_lock_is_exclusive_and_breaks_stale_holders(tmp_path):
    import os
    import time

    from schub.locking import LockTimeout, exclusive

    lock = tmp_path / "x.lock"
    with exclusive(lock):
        assert lock.exists()
        with pytest.raises(LockTimeout):
            with exclusive(lock, wait_s=0.3):
                pass
    assert not lock.exists()
    lock.write_text("dead holder")
    old = time.time() - 1000
    os.utime(lock, (old, old))
    with exclusive(lock, wait_s=0.3, stale_after_s=10):
        pass


def test_provenance_ids_are_stable_hashes():
    from schub.bricks import REGISTRY
    from schub.provenance import code_id, env_id

    assert env_id() == env_id() and len(env_id()) == 16
    ids = {code_id(spec) for spec in REGISTRY.values()}
    assert len(ids) == len(REGISTRY)


def test_notebook_refreshes_until_the_student_edits_it(tmp_path):
    import json as _json

    from schub.notebook import write_notebook

    manifest = RunManifest(run_id="20260923-100000-abcdef-0000", plan_id="abcdef123456", created_at="now",
                           dataset="/l/data.h5ad", steps=(), schub_version="0.3.0")
    running = RunResults(run_id=manifest.run_id, state="RUNNING", final_output=None, steps=())
    done = running.model_copy(update={"state": "COMPLETED", "final_output": "/l/out.h5ad"})
    path = write_notebook(tmp_path, manifest, running)
    assert "None" in path.read_text()
    write_notebook(tmp_path, manifest, done)
    assert "/l/out.h5ad" in path.read_text()  # untouched notebook: refreshed
    edited = _json.loads(path.read_text())
    edited["cells"].append({"cell_type": "code", "source": ["my analysis"], "metadata": {}, "outputs": [], "execution_count": None})
    path.write_text(_json.dumps(edited))
    write_notebook(tmp_path, manifest, running)
    assert "my analysis" in path.read_text()  # edited: kept


def test_storage_hiccups_are_retried_but_real_errors_are_not():
    from schub.execute import _run_with_retries, transient

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("Can't synchronously write data (file write failed: errno = 14, error message = 'Bad address')")
        return {"ok": True}

    assert _run_with_retries(flaky, pause_s=0) == {"ok": True} and len(calls) == 3
    assert transient(OSError(14, "Bad address")) and not transient(OSError(2, "No such file"))
    assert not transient(ValueError("errno = 14,"))
    with pytest.raises(ValueError):
        _run_with_retries(lambda: (_ for _ in ()).throw(ValueError("bad params")), pause_s=0)
