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


def test_notebook_is_valid_python():
    manifest = RunManifest(
        run_id="20260923-100000-abcdef", plan_id="abcdef123456", created_at="now",
        dataset="/l/data.h5ad", steps=(), schub_version="0.1.0",
    )
    results = RunResults(
        run_id=manifest.run_id, state="COMPLETED", final_output="/l/out.h5ad",
        steps=(StepResults(index=1, brick="pseudobulk_de", summary={"a|b": 1}, files=("/l/r/de_all.csv",)),),
    )
    source = render_notebook(manifest, results)
    ast.parse(source)
    assert "'/l/out.h5ad'" in source and "'/l/r/de_all.csv'" in source


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
