"""What the student and the assistants hear while the setup's background part runs (setup-extras.json)."""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from schub import setup_status
from schub.bench.checks.training import check_gpu, NoParams
from schub.config import Settings
from schub.dashboard.collect_journal import _bench_alerts

SCVI_MISSING = "ModuleNotFoundError: No module named 'scvi'"


def write(settings: Settings, **fields: object) -> None:
    (settings.root / "setup-extras.json").write_text(json.dumps(fields))


def just_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_nothing_to_say_without_a_background_part_or_once_it_is_done(settings: Settings) -> None:
    assert setup_status.read(settings) is None and setup_status.note(settings) == ""
    write(settings, state="done", job="4242")
    assert setup_status.note(settings) == "" and not setup_status.pending(settings)
    assert setup_status.missing_module_hint(settings, SCVI_MISSING) == ""


def test_a_broken_or_odd_file_is_ignored(settings: Settings) -> None:
    (settings.root / "setup-extras.json").write_text("{half")
    assert setup_status.read(settings) is None
    (settings.root / "setup-extras.json").write_text("[1, 2]")
    assert setup_status.read(settings) is None
    write(settings, state="queued", job=4242, extra="ignored")
    assert setup_status.read(settings) == setup_status.SetupStatus(state="queued", job="4242")


def test_a_shared_library_environment_is_not_what_the_file_is_about(settings: Settings) -> None:
    write(settings, state="failed", job="4242", step="the GPU check")  # left from a private environment
    library = dataclasses.replace(settings, python=settings.library / "envs" / "current" / "bin" / "python")
    assert setup_status.note(settings) != "" and setup_status.note(library) == ""


def test_queued_and_running_say_what_is_coming_and_what_it_takes(settings: Settings) -> None:
    write(settings, state="queued", job="4242")
    queued = setup_status.note(settings)
    assert setup_status.pending(settings)
    assert queued.startswith("The deep-learning tools (torch, scvi-tools) install in the background (job 4242, queued)")
    assert "everything else works now" in queued and "job slots" in queued
    write(settings, state="running", job="4242", step="the deep-learning tools (torch, scvi-tools)",
          started="2026-09-30T16:20:05+0400")
    running = setup_status.note(settings)
    assert running.startswith("Still installing the deep-learning tools (torch, scvi-tools) in the background since "
                              "16:20 (job 4242: now the deep-learning tools")
    assert "a new Slurm job may wait for a free job slot" in running


def test_a_failed_part_says_where_it_stopped_and_how_to_finish_it(settings: Settings) -> None:
    write(settings, state="failed", job="4242", step="the GPU check", error="torch is installed but saw no GPU (log: x)")
    text = setup_status.note(settings)
    assert "stopped at the GPU check (torch is installed but saw no GPU (log: x))" in text
    assert "retry cluster" in text and "start.cmd" in text and "pilot owner" in text
    assert not setup_status.pending(settings)
    assert setup_status.missing_module_hint(settings, SCVI_MISSING).endswith("Run this cell again after that.")


def test_a_job_that_left_the_queue_is_not_called_on_its_way(settings: Settings) -> None:
    write(settings, state="running", job="4242", updated="2026-09-30T16:20:05+0400")
    assert setup_status.note(settings, queue={"4242", "17"}).startswith("Still installing")
    text = setup_status.note(settings, queue={"17"})
    assert text.startswith("The background install of the deep-learning tools (torch, scvi-tools; job 4242) ended "
                           "without finishing (its last word: 2026-09-30 16:20)")
    assert not setup_status.pending(settings, queue={"17"})
    write(settings, state="queued", job="4242", updated=just_now())  # just queued: a queue listed before may miss it
    assert setup_status.pending(settings, queue=set())
    assert setup_status.pending(settings, queue=None)  # Slurm did not answer: nobody can tell


def test_without_the_queue_hours_of_silence_count_against_it_with_the_queue_they_do_not(settings: Settings) -> None:
    write(settings, state="queued", job="4242", updated="2026-09-30T09:00:00+0400")
    assert setup_status.pending(settings, queue={"4242"})  # waiting for a free slot for hours: still on its way
    assert setup_status.note(settings, queue={"4242"}).startswith("The deep-learning tools (torch, scvi-tools) install")
    write(settings, state="running", job="4242", step="the deep-learning tools (torch, scvi-tools)",
          updated="2026-09-30T09:00:00+0400")  # its job may have ended without a word (killed)
    text = setup_status.note(settings)
    assert "has not reported since 2026-09-30 09:00. If that job is no longer in the queue" in text
    assert "retry cluster" in text and not setup_status.pending(settings)
    assert setup_status.missing_module_hint(settings, SCVI_MISSING).endswith("Run this cell again after that.")
    fresh = setup_status.SetupStatus(state="queued", updated=just_now())
    assert not setup_status.stale(fresh)
    assert setup_status.stale(fresh, now=datetime.now(timezone.utc) + timedelta(hours=4))
    assert not setup_status.stale(setup_status.SetupStatus(state="queued", updated="not a time"))


@pytest.mark.parametrize("error, hinted", [
    (SCVI_MISSING, True),
    ("ModuleNotFoundError: No module named 'torch'", True),
    ("ModuleNotFoundError: No module named 'scvi.model'", True),  # a submodule of a coming package
    ("ModuleNotFoundError: No module named 'lightning'", True),
    ("ModuleNotFoundError: No module named 'torchvision'", False),  # not in the ml extra: it will not come
    ("ModuleNotFoundError: No module named 'jax'", False),
    ("ImportError: cannot import name 'SCVI' from 'scvi'", False),  # installed but broken: not ours to explain
    ("NameError: name 'torch' is not defined", False),
])
def test_a_cell_hint_only_for_the_modules_the_background_part_adds(settings: Settings, error: str,
                                                                   hinted: bool) -> None:
    write(settings, state="queued", job="4242")
    hint = setup_status.missing_module_hint(settings, error)
    assert bool(hint) is hinted
    if hinted:
        assert hint.startswith("The deep-learning tools") and hint.endswith("Run this cell again when it has finished.")


def test_the_dashboard_shows_it_first_among_the_alerts(settings: Settings) -> None:
    assert _bench_alerts(settings, {}, None) == []
    write(settings, state="queued", job="4242", updated=just_now())
    assert _bench_alerts(settings, {}, None, {"4242"})[0].startswith("The deep-learning tools (torch, scvi-tools)")
    write(settings, state="queued", job="4242", updated="2026-09-30T09:00:00+0400")
    assert "ended without finishing" in _bench_alerts(settings, {}, None, set())[0]


def test_the_queue_is_asked_only_while_the_background_part_is_on_its_way(settings: Settings) -> None:
    from schub.slurm import SlurmError

    class Queue:
        def __init__(self, ids: tuple[str, ...] = (), down: bool = False) -> None:
            self.ids, self.down, self.asked = ids, down, 0

        def my_jobs(self):
            self.asked += 1
            if self.down:
                raise SlurmError("squeue: error: Socket timed out")
            return [type("Job", (), {"job_id": i})() for i in self.ids]

    idle = Queue(("7",))
    assert setup_status.live_queue(settings, idle) is None and idle.asked == 0  # no background part
    write(settings, state="done", job="4242")
    assert setup_status.live_queue(settings, idle) is None and idle.asked == 0
    write(settings, state="running", job="4242")
    assert setup_status.live_queue(settings, Queue(("4242", "7"))) == {"4242", "7"}
    assert setup_status.live_queue(settings, Queue(down=True)) is None  # Slurm did not answer: nobody can tell


def test_a_starter_dataset_the_setup_could_not_download_is_named_until_it_is_there(settings: Settings,
                                                                                   write_h5ad) -> None:
    from schub.datasets import write_catalog_entry
    from tests.conftest import library_datasets, make_adata

    (settings.root / "setup-datasets-missing.txt").write_text("pbmc3k kang2018\n")
    assert setup_status.note(settings) == ("The setup could not download the starter data pbmc3k, kang2018: running "
                                           "the setup's cluster step again downloads it.")
    directory = library_datasets(settings) / "pbmc3k"
    write_catalog_entry(directory, {"title": "PBMC"})
    write_h5ad(make_adata(), directory=directory)  # (fetched by hand since)
    assert "starter data kang2018:" in setup_status.note(settings)
    write(settings, state="queued", job="4242")
    both = setup_status.note(settings)
    assert both.startswith("The deep-learning tools") and both.endswith("again downloads it.")
    assert "kang2018" not in setup_status.install_note(settings)


def test_the_gpu_check_says_torch_is_on_its_way(settings: Settings, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)  # torch not installed (yet)
    monkeypatch.delenv("SCHUB_CHECK_PYTHON", raising=False)
    monkeypatch.delenv("SCHUB_PYTHON", raising=False)
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    assert check_gpu(lambda p: settings.root / p, NoParams()).message == "torch is not installed in this environment"
    write(settings, state="running", job="4242", step="the deep-learning tools (torch, scvi-tools)")
    result = check_gpu(lambda p: settings.root / p, NoParams())
    assert result.status == "fail" and result.message.startswith("torch is not installed in this environment yet. "
                                                                  "Still installing the deep-learning tools")
