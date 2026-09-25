from __future__ import annotations

import json

import numpy as np
import pytest
from scipy import sparse

from schub.aggregate import pseudobulk
from schub.audit import audited
from schub.config import load_settings


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


def test_import_bench_works_unless_the_project_has_its_own(tmp_path, monkeypatch):
    """Agents write `import bench` (three cells on the test root failed on it)."""
    import sys

    from schub.bench import kernel_api

    monkeypatch.delitem(sys.modules, "bench", raising=False)
    kernel_api.alias()
    import bench

    assert bench is kernel_api
    monkeypatch.delitem(sys.modules, "bench")
    (tmp_path / "bench.py").write_text("OWN = True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    kernel_api.alias()
    assert "bench" not in sys.modules  # the project's own bench.py is left to import
    import bench as own

    assert own.OWN is True


def test_an_interrupted_build_stops_everything_it_started(tmp_path):
    """A cell interrupted during bench.packages / import_seurat must not leave uv or micromamba running."""
    import time

    import pytest

    from schub.streaming import run_streamed

    marker = tmp_path / "still-running"

    class Interrupting:
        def write(self, line):
            raise KeyboardInterrupt

    script = f"(sleep 2; touch {marker}) & echo started; wait"
    with pytest.raises(KeyboardInterrupt):
        run_streamed(["bash", "-c", script], Interrupting())
    time.sleep(3)
    assert not marker.exists()
    assert run_streamed(["bash", "-c", "echo one; echo two; exit 3"], tmp_path.joinpath("log").open("w"), keep=1) == \
        (3, ["two\n"])
