"""sc-hub's own submission queue: above the per-user cap of active pipelines a plan
waits in sc-hub instead of failing, and a tiny Slurm job ("pump") submits it when a
pipeline ends. No daemon: Slurm dependencies wake the pump."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from schub.config import Limits
from schub.datasets import write_catalog_entry
from schub.queue import QueuedSubmission
from schub.runs import RunManifest
from schub.service import Hub
from schub.slurm import JobSpec, Resources, Slurm, render_script

from .conftest import library_datasets, make_adata

QC = {"brick": "qc_filter", "params": {"max_pct_mt": 100}}


@pytest.fixture
def hub(settings, cluster, ctx, write_h5ad):
    directory = library_datasets(settings) / "kang2018"
    write_catalog_entry(directory, {"title": "Kang"})
    write_h5ad(make_adata(), directory=directory)
    return Hub(replace(settings, limits=Limits(max_active_runs=1)), Slurm(cluster))


def plan(hub, min_genes):
    return hub.plan("kang2018", [{**QC, "params": {**QC["params"], "min_genes": min_genes}}, {"brick": "normalize_embed"}])


def pump_jobs(cluster):
    return [j for j, name in cluster.names.items() if name == "schub-pump" and cluster.jobs[j] in cluster.ACTIVE]


def test_above_the_cap_a_plan_waits_in_sc_hub_and_a_pump_is_scheduled(hub, cluster):
    first = hub.submit(plan(hub, 10).plan_id)
    assert isinstance(first, RunManifest)
    waiting = hub.submit(plan(hub, 20).plan_id)
    assert isinstance(waiting, QueuedSubmission) and waiting.position == 1
    assert "no need to submit it again" in waiting.message
    (pump,) = pump_jobs(cluster)
    last = first.steps[-1].job_id
    assert cluster.dependencies(pump) == f"afterany:{last}"  # woken when the active pipeline ends
    assert "--kill-on-invalid-dep" not in cluster.scripts[pump]
    assert "schub.cli pump" in cluster.scripts[pump].replace("'", "")


def test_queueing_is_idempotent_and_keeps_one_pump(hub, cluster):
    hub.submit(plan(hub, 10).plan_id)
    second, third = plan(hub, 20), plan(hub, 30)
    assert hub.submit(second.plan_id).position == 1
    again = hub.submit(second.plan_id)
    assert isinstance(again, QueuedSubmission) and again.position == 1
    assert hub.submit(third.plan_id).position == 2
    assert len(pump_jobs(cluster)) == 1
    assert [q.plan_id for q in hub.queued()] == [second.plan_id, third.plan_id]


def test_the_pump_submits_in_order_when_a_slot_frees(hub, cluster):
    first = hub.submit(plan(hub, 10).plan_id)
    second, third = plan(hub, 20), plan(hub, 30)
    hub.submit(second.plan_id)
    hub.submit(third.plan_id)
    assert hub.pump().submitted == ()  # nothing ended yet
    for step in first.steps:
        cluster.jobs[step.job_id] = "COMPLETED"
    result = hub.pump()
    assert [r.plan_id for r in result.submitted] == [second.plan_id]
    assert [q.plan_id for q in hub.queued()] == [third.plan_id]
    assert len(pump_jobs(cluster)) == 1  # still waiting for a slot for the third


def test_a_queued_plan_that_can_no_longer_run_is_set_aside_with_the_reason(hub, cluster, settings):
    first = hub.submit(plan(hub, 10).plan_id)
    waiting = plan(hub, 20)
    hub.submit(waiting.plan_id)
    make_adata(n_obs=61, seed=9).write_h5ad(library_datasets(settings) / "kang2018" / "data.h5ad")
    for step in first.steps:
        cluster.jobs[step.job_id] = "COMPLETED"
    result = hub.pump()
    assert result.submitted == () and hub.queued() == []
    (failed,) = result.failed
    assert failed.plan_id == waiting.plan_id and "changed after planning" in failed.message
    assert [f.plan_id for f in hub.queue.failed()] == [waiting.plan_id]


def test_a_queued_plan_can_be_cancelled(hub, cluster):
    hub.submit(plan(hub, 10).plan_id)
    waiting = plan(hub, 20)
    hub.submit(waiting.plan_id)
    assert hub.cancel_queued(waiting.plan_id) is True
    assert hub.queued() == [] and hub.cancel_queued(waiting.plan_id) is False


def test_pump_job_dependency_is_any_of_the_pipelines_last_steps():
    spec = JobSpec(name="schub-pump", partition="ws-ia", resources=Resources(cpus=1, mem_gb=1, time_min=10),
                   log_path=Path("/tmp/p.log"), workdir=Path("/tmp"), command=("python", "-m", "schub.cli", "pump"),
                   after_any=("11", "22"))
    script = render_script(spec)
    assert "#SBATCH --dependency=afterany:11?afterany:22" in script and "kill-on-invalid-dep" not in script


def finish_all(cluster, run):
    for step in run.steps:
        cluster.jobs[step.job_id] = "COMPLETED"


def test_a_running_pump_does_not_count_so_no_wakeup_is_lost(hub, cluster):
    hub.submit(plan(hub, 10).plan_id)
    hub.submit(plan(hub, 20).plan_id)
    (first_pump,) = pump_jobs(cluster)
    cluster.jobs[first_pump] = "RUNNING"  # it may have found the queue empty and be exiting
    hub.submit(plan(hub, 30).plan_id)
    assert len([j for j in pump_jobs(cluster) if cluster.jobs[j] == "PENDING"]) == 1


def test_an_unreadable_entry_is_set_aside_and_does_not_block_the_queue(hub, cluster):
    first = hub.submit(plan(hub, 10).plan_id)
    waiting = plan(hub, 20)
    hub.submit(waiting.plan_id)
    (hub.queue.dir / "0000000000000000001-badbadbadbad.json").write_text("")  # a truncated write
    finish_all(cluster, first)
    result = hub.pump()
    assert [r.plan_id for r in result.submitted] == [waiting.plan_id]
    assert "unreadable" in result.failed[0].message


def test_passing_errors_keep_the_plan_and_permanent_ones_set_it_aside(hub, cluster, monkeypatch):
    from schub import queue as queue_module
    from schub.slurm import SlurmError

    first = hub.submit(plan(hub, 10).plan_id)
    waiting = plan(hub, 20)
    hub.submit(waiting.plan_id)
    finish_all(cluster, first)
    calls = []

    def flaky(plan_id, force_new):
        calls.append(plan_id)
        raise SlurmError("sbatch: error: Socket timed out on send/recv operation")

    monkeypatch.setattr(hub.queue, "_submit", flaky)
    for attempt in range(1, 20):  # a long Slurm outage: the plan keeps waiting
        assert hub.pump().failed == () and hub.queued()[0].attempts == attempt
    now = queue_module.time.time()
    monkeypatch.setattr(queue_module.time, "time", lambda: now + queue_module.GIVE_UP_AFTER_S + 1)
    assert "gave up after 20 attempts" in hub.pump().failed[0].message and hub.queued() == []
    assert len(calls) == 20


def test_plans_are_submitted_first_in_first_out(hub, cluster):
    first = hub.submit(plan(hub, 10).plan_id)
    older = plan(hub, 20)
    hub.submit(older.plan_id)
    finish_all(cluster, first)  # a slot frees; the pump has not run yet
    newer = hub.submit(plan(hub, 30).plan_id)
    assert newer.status == "queued"  # it does not jump ahead of the plan that waited
    assert older.plan_id in {r.plan_id for r in hub.runs(10)}


def test_a_plan_whose_steps_are_all_cached_never_waits(hub, cluster, settings):
    from schub.stepfile import SUCCESS

    done = plan(hub, 10)
    run = hub.submit(done.plan_id)
    for step in run.steps:
        (Path(step.step_dir) / SUCCESS).write_text("ok")
    hub.submit(plan(hub, 20).plan_id)  # holds the only slot
    again = hub.submit(hub.plan("kang2018", [{**QC, "params": {**QC["params"], "min_genes": 10}},
                                             {"brick": "normalize_embed"}]).plan_id)
    assert isinstance(again, RunManifest)


def test_a_slurm_hiccup_while_scheduling_the_pump_keeps_the_plan_queued(hub, cluster):
    hub.submit(plan(hub, 10).plan_id)
    cluster.fail_sbatch_at = len(cluster.jobs)  # the next sbatch (the pump) fails
    waiting = hub.submit(plan(hub, 20).plan_id)
    assert waiting.status == "queued" and pump_jobs(cluster) == []
    cluster.fail_sbatch_at = None
    hub.queue.ensure_pump()  # the next status check or dashboard build does this
    assert len(pump_jobs(cluster)) == 1


def test_a_branch_that_waited_through_an_update_runs_as_it_is_now(hub, cluster, monkeypatch):
    from schub.projects import BranchSpec

    hub.create_project("p", question="q")
    hub.save_branch("p", "a", BranchSpec(dataset="kang2018", steps=(QC, {"brick": "normalize_embed"})))
    hub.save_branch("p", "b", BranchSpec(from_branch="a", overrides={"normalize_embed": {"n_pcs": 10}}))
    first = hub.submit(hub.plan_branch("p", "a").plan_id)
    old = hub.plan_branch("p", "b")
    assert hub.submit(old.plan_id).status == "queued"
    monkeypatch.setattr("schub.service.env_id", lambda: "upgraded-packages")  # sc-hub updated meanwhile
    assert hub.submit(hub.plan_branch("p", "b").plan_id).position == 1  # the branch waits once
    finish_all(cluster, first)
    (run,) = hub.pump().submitted
    assert run.branch == "b" and run.plan_id != old.plan_id  # the new plan, not the stale one


def test_a_stale_one_off_plan_is_refused_with_the_reason(hub, cluster, monkeypatch):
    from schub.runs import PlanRejected

    stale = plan(hub, 10)
    monkeypatch.setattr("schub.service.code_id", lambda spec: "edited-code")
    with pytest.raises(PlanRejected, match="changed the code of normalize_embed, qc_filter"):
        hub.submit(stale.plan_id)
