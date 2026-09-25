"""What the student sees when something needs them: an engine that stopped answering, a job that ended
without its result, a Slurm reason in words, and cells counted only when they ran."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schub.bench.engines.cooldown import Cooldown
from schub.bench.journal import Journal
from schub.bench.models import CellEntry, JobRef
from schub.bench.results import cell_result
from schub.config import Settings
from schub.dashboard.collect_journal import _engine_alerts, journal_cards
from schub.dashboard.views_activity import reason
from schub.dashboard.views_journal import _badges, _failed
from schub.projects import ProjectStore


def test_an_engine_that_stopped_answering_is_an_alert(settings: Settings) -> None:
    settings.bench_dir.mkdir(parents=True, exist_ok=True)
    store = Cooldown(settings.bench_dir / "engine-cooldown.json")
    store.mark("codex", "usage limit reached")  # a usage limit is no news for the student
    assert _engine_alerts(settings) == []
    for _ in range(2):
        store.failing("claude", "Invalid API key", login="old")
        state = store._state()
        state["claude:failures"]["last"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        (settings.bench_dir / "engine-cooldown.json").write_text(__import__("json").dumps(state))
    [alert] = _engine_alerts(settings)
    assert alert.startswith("Claude Code on the cluster is not answering") and "retry agents" in alert and "`" not in alert


def entry(state: str) -> dict:
    return {"checks": [], "jobs": [{"job_id": "901", "state": state}], "data_scope": "unknown", "setup": False,
            "bricks": [], "status": "ok"}


def test_a_job_that_ended_without_its_result_is_red_and_short() -> None:
    for state, word in (("OUT_OF_MEMORY", "out of memory"), ("NOT_STARTED", "not started"), ("TIMEOUT", "timeout")):
        html = _badges(entry(state))
        assert 'class="pill FAILED"' in html and f"job 901 {word}" in html and _failed(entry(state))
    assert 'class="pill COMPLETED"' in _badges(entry("COMPLETED")) and not _failed(entry("COMPLETED"))
    assert not _failed(entry("CANCELLED"))  # cancelled on purpose: grey, not a failure


def test_the_agent_hears_why_a_job_left_nothing() -> None:
    cell = CellEntry(ref="p#c0001", project="p", cid="c0001", why="w", expect="e", code="x", created="t",
                     status="ok", jobs=(JobRef(job_id="901", state="OUT_OF_MEMORY"),))
    hint = cell_result(cell, "", "", ()).hint
    assert hint.startswith("job 901 ran out of memory: send it again with a larger --mem")


def test_slurm_reasons_in_words() -> None:
    assert reason("(ReqNodeNotAvail, UnavailableNodes:gpu-01)").startswith("its nodes are unavailable")
    assert reason("(launch failed requeued held)").startswith("held after a failed start")
    assert reason("(JobHeldAdmin)").startswith("held by the cluster's admins") and reason("(Odd)") == "Odd"


def test_cells_that_never_ran_are_not_counted(settings: Settings) -> None:
    ProjectStore(settings).create("p")
    journal = Journal(settings.projects_dir / "p", "p")
    ran, withdrawn = journal.allocate("c"), journal.allocate("c")
    journal.write_cell(CellEntry(ref=f"p#{ran}", project="p", cid=ran, why="w", expect="e", code="x",
                                 created=journal.now(), status="ok"))
    [card] = [c for c in journal_cards(settings, queue=()) if c.project == "p"]
    assert card.cells == 1 and card.total_entries == 1 and withdrawn == "c0002"


def test_every_ending_without_a_result_is_explained() -> None:
    from schub.bench.jobs import bad_ending

    assert bad_ending("ENDED (never started: cancelled or not launched while queued)") == bad_ending("NOT_STARTED")
    assert bad_ending("FAILED").startswith("failed: its log and this entry's outputs")
    assert bad_ending("ENDED (no result yet)").startswith("ended without reporting")
    assert bad_ending("COMPLETED") is None and bad_ending("RUNNING") is None and bad_ending("CANCELLED") is None
