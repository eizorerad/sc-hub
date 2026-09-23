from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schub.config import Limits
from schub.h5ad_profile import profile_h5ad
from schub.hashing import file_fingerprint
from schub.planner import StepRequest, build_plan
from schub.runs import LimitExceeded, RunError, RunNotFound, RunStore, overall_state
from schub.slurm import Slurm, SlurmError
from schub.stepfile import ERROR_FILE, SUCCESS, SUMMARY_FILE, StepFile

from .conftest import make_adata

QC = StepRequest(brick="qc_filter")
NORM = StepRequest(brick="normalize_embed")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def dataset(write_h5ad, settings):
    return write_h5ad(make_adata(), directory=settings.data_dir)


@pytest.fixture
def plan_for(dataset, ctx):
    def _plan(*steps):
        return build_plan(profile_h5ad(dataset), file_fingerprint(dataset), list(steps), ctx)

    return _plan


@pytest.fixture
def store(settings, cluster):
    return RunStore(settings, Slurm(cluster), clock=Clock())


def test_submit_builds_a_dependency_chain(store, cluster, plan_for, settings):
    manifest = store.submit(plan_for(QC, NORM))
    first, second = manifest.steps
    assert first.job_id and second.job_id and not first.reused
    assert cluster.dependencies(first.job_id) is None
    assert cluster.dependencies(second.job_id) == f"afterok:{first.job_id}"
    step_file = StepFile.model_validate_json((Path(second.step_dir) / "step.json").read_text())
    assert step_file.input == str(Path(first.step_dir) / "output.h5ad")
    assert step_file.state_in.flags == ("qc",)
    run_dir = settings.runs_dir / manifest.run_id
    assert (run_dir / "01_qc_filter").resolve() == Path(first.step_dir).resolve()
    assert str(settings.python) in cluster.scripts[first.job_id]


def test_resubmitting_the_same_plan_returns_the_same_run(store, cluster, plan_for):
    plan = plan_for(QC, NORM)
    first = store.submit(plan)
    second = store.submit(plan)
    assert second.run_id == first.run_id
    assert len(cluster.jobs) == 2


def test_failed_run_is_resubmitted_and_completed_steps_are_reused(store, cluster, plan_for):
    plan = plan_for(QC, NORM)
    first = store.submit(plan)
    qc, norm = first.steps
    (Path(qc.step_dir) / SUCCESS).write_text("done")
    cluster.jobs[qc.job_id] = "COMPLETED"
    cluster.jobs[norm.job_id] = "FAILED"
    (Path(norm.step_dir) / ERROR_FILE).write_text(json.dumps({"message": "boom"}))
    status = store.status(first.run_id)
    assert status.state == "FAILED" and status.steps[1].message == "boom"
    retry = store.submit(plan)
    assert retry.run_id != first.run_id
    assert retry.steps[0].reused and retry.steps[0].job_id is None
    assert cluster.dependencies(retry.steps[1].job_id) is None
    assert not (Path(norm.step_dir) / ERROR_FILE).exists()


def test_changed_params_reuse_the_cached_prefix(store, cluster, plan_for):
    first = store.submit(plan_for(QC, NORM))
    (Path(first.steps[0].step_dir) / SUCCESS).write_text("done")
    changed = store.submit(plan_for(QC, StepRequest(brick="normalize_embed", params={"n_top_genes": 500})))
    assert changed.steps[0].reused
    assert changed.steps[1].step_key != first.steps[1].step_key


def test_queued_step_from_another_run_is_not_duplicated(store, cluster, plan_for):
    first = store.submit(plan_for(QC))
    second = store.submit(plan_for(QC, NORM))
    assert second.steps[0].reused and second.steps[0].job_id == first.steps[0].job_id
    assert cluster.dependencies(second.steps[1].job_id) == f"afterok:{first.steps[0].job_id}"
    assert len(cluster.jobs) == 2


def test_plans_with_errors_are_refused(store, plan_for):
    with pytest.raises(RunError, match="has errors"):
        store.submit(plan_for(StepRequest(brick="annotate_celltypist")))


def test_active_pipeline_limit(settings, cluster, plan_for):
    limited = RunStore(replace(settings, limits=Limits(max_active_runs=1)), Slurm(cluster), clock=Clock())
    limited.submit(plan_for(QC))
    with pytest.raises(LimitExceeded):
        limited.submit(plan_for(StepRequest(brick="qc_filter", params={"min_genes": 10})))


def test_partial_submission_is_rolled_back(store, cluster, plan_for):
    cluster.fail_sbatch_at = 1
    with pytest.raises(SlurmError):
        store.submit(plan_for(QC, NORM))
    assert list(cluster.jobs.values()) == ["CANCELLED"]


def test_status_logs_results_and_cancel(store, cluster, plan_for):
    manifest = store.submit(plan_for(QC, NORM))
    qc, norm = manifest.steps
    cluster.jobs[qc.job_id] = "RUNNING"
    assert store.status(manifest.run_id).state == "RUNNING"
    assert "no log yet" in store.logs(manifest.run_id, 1)
    (Path(qc.step_dir) / f"slurm-{qc.job_id}.log").write_text("\n".join(f"line {i}" for i in range(100)))
    assert store.logs(manifest.run_id, 1, lines=2) == "line 98\nline 99"
    results_dir = Path(qc.step_dir) / "results"
    results_dir.mkdir()
    (results_dir / SUMMARY_FILE).write_text(json.dumps({"cells_final": 55}))
    (Path(qc.step_dir) / "output.h5ad").write_text("x")
    results = store.results(manifest.run_id)
    assert results.steps[0].summary == {"cells_final": 55}
    assert results.final_output.endswith("output.h5ad")
    cancelled = store.cancel(manifest.run_id)
    assert cancelled.state == "FAILED"
    assert cluster.jobs[norm.job_id] == "CANCELLED"
    with pytest.raises(RunError):
        store.step_record(manifest.run_id, 9)


def test_listing_and_missing_runs(store, plan_for):
    assert store.list_runs() == []
    a = store.submit(plan_for(QC))
    b = store.submit(plan_for(StepRequest(brick="qc_filter", params={"min_genes": 5})))
    assert [m.run_id for m in store.list_runs()] == [b.run_id, a.run_id]
    with pytest.raises(RunNotFound):
        store.load("20260101-000000-abcdef-0000")


def test_missing_cache_marks_step(store, cluster, plan_for):
    manifest = store.submit(plan_for(QC))
    (Path(manifest.steps[0].step_dir) / SUCCESS).write_text("done")
    reused = store.submit(plan_for(QC), force_new=True)
    (Path(reused.steps[0].step_dir) / SUCCESS).unlink()
    assert store.status(reused.run_id).steps[0].state == "MISSING"


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["COMPLETED", "COMPLETED"], "COMPLETED"),
        (["COMPLETED", "RUNNING"], "RUNNING"),
        (["PENDING", "UNKNOWN"], "FAILED"),
        (["PENDING", "CONFIGURING"], "PENDING"),
        (["COMPLETED", "OUT_OF_MEMORY"], "FAILED"),
        ([], "PENDING"),
    ],
)
def test_overall_state(states, expected):
    from schub.runs import StepStatus

    steps = tuple(StepStatus(index=i, brick="b", state=s, job_id=None, reused=False) for i, s in enumerate(states))
    assert overall_state(steps) == expected


def test_concurrent_submissions_of_one_plan_do_not_duplicate_jobs(settings, cluster, plan_for):
    import threading

    plan = plan_for(QC, NORM)
    stores = [RunStore(settings, Slurm(cluster)) for _ in range(4)]
    results = []
    threads = [threading.Thread(target=lambda s=s: results.append(s.submit(plan))) for s in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(cluster.jobs) == 2
    assert len({m.run_id for m in results}) == 1


def test_lock_timeout_is_a_run_error(store, plan_for, settings, monkeypatch):
    import schub.runs as runs_module
    from schub.locking import LockTimeout

    def busy(*_args, **_kwargs):
        raise LockTimeout("busy")

    monkeypatch.setattr(runs_module, "exclusive", busy)
    with pytest.raises(RunError, match="busy"):
        store.submit(plan_for(QC))


def test_cancel_keeps_jobs_another_run_depends_on(store, cluster, plan_for):
    first = store.submit(plan_for(QC))
    second = store.submit(plan_for(QC, NORM))
    shared_job = first.steps[0].job_id
    assert second.steps[0].job_id == shared_job
    store.cancel(first.run_id)
    assert cluster.jobs[shared_job] == "PENDING"
    store.cancel(second.run_id)
    assert cluster.jobs[second.steps[1].job_id] == "CANCELLED"


def test_live_job_below_a_fresh_step_is_not_reused(store, cluster, plan_for):
    first = store.submit(plan_for(QC, NORM))
    qc, norm = first.steps
    cluster.jobs[qc.job_id] = "FAILED"  # norm is still PENDING, doomed by its dependency
    retry = store.submit(plan_for(QC, NORM))
    assert not retry.steps[0].reused
    assert not retry.steps[1].reused and retry.steps[1].job_id != norm.job_id
    assert cluster.dependencies(retry.steps[1].job_id) == f"afterok:{retry.steps[0].job_id}"


def test_completed_without_marker_and_unknown_jobs_fail(store, cluster, plan_for):
    manifest = store.submit(plan_for(QC))
    cluster.jobs[manifest.steps[0].job_id] = "COMPLETED"
    status = store.status(manifest.run_id)
    assert status.state == "FAILED" and status.steps[0].state == "LOST"
    del cluster.jobs[manifest.steps[0].job_id]
    assert store.status(manifest.run_id).steps[0].state == "UNKNOWN"


def test_idempotent_resubmit_when_sacct_is_unreachable(store, cluster, plan_for):
    cluster.sacct_down = True
    plan = plan_for(QC, NORM)
    first = store.submit(plan)
    assert store.submit(plan).run_id == first.run_id
    (Path(first.steps[0].step_dir) / SUCCESS).write_text("done")
    cluster.jobs[first.steps[0].job_id] = "COMPLETED"
    cluster.jobs[first.steps[1].job_id] = "RUNNING"
    assert store.status(first.run_id).state == "RUNNING"
