from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from schub.bench.journal import Journal, JournalError, parse_ref
from schub.bench.models import Actor, CellEntry, CheckResult, JobRef


class Ticks:
    """A clock that moves one second per call."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-09-24T10:{self.n // 60:02d}:{self.n % 60:02d}.000+00:00"


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "projects" / "demo", "demo", now=Ticks())


def cell(journal: Journal, cid: str, status: str = "running", **extra) -> CellEntry:
    return CellEntry(ref=f"demo#{cid}", project="demo", cid=cid, why="look", expect="a table", code="1+1",
                     created=journal.now(), status=status, **extra)


def test_ids_are_sequential_and_never_reused(journal: Journal) -> None:
    assert [journal.allocate("c") for _ in range(3)] == ["c0001", "c0002", "c0003"]
    assert journal.allocate("n") == "n0001"
    assert journal.allocate("c") == "c0004"


def _allocate_many(folder: str, count: int, out) -> None:
    journal = Journal(Path(folder), "demo")
    out.put([journal.allocate("c") for _ in range(count)])


def test_two_processes_never_get_the_same_id(tmp_path: Path) -> None:
    folder = tmp_path / "projects" / "demo"
    queue: multiprocessing.Queue = multiprocessing.get_context("spawn").Queue()
    workers = [multiprocessing.get_context("spawn").Process(target=_allocate_many, args=(str(folder), 20, queue))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    ids = queue.get(timeout=60) + queue.get(timeout=60)
    for worker in workers:
        worker.join(timeout=60)
    assert len(ids) == len(set(ids)) == 40


def test_a_finished_cell_never_changes(journal: Journal) -> None:
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid))
    journal.write_cell(cell(journal, cid, status="ok"))
    with pytest.raises(JournalError, match="final"):
        journal.write_cell(cell(journal, cid, status="error"))
    assert journal.cell(cid).status == "ok"


def test_addenda_merge_into_the_cell(journal: Journal) -> None:
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid, status="ok", jobs=(JobRef(job_id="77", state="PENDING"),)))
    assert journal.add_addendum(cid, "job-77", {"kind": "job", "job": {"job_id": "77", "state": "COMPLETED", "exit_code": 0}})
    assert not journal.add_addendum(cid, "job-77", {"kind": "job", "job": {"job_id": "77", "state": "FAILED"}})
    journal.add_addendum(cid, "check-counts", {"kind": "check", "check": CheckResult(name="counts", status="pass").model_dump()})
    merged = journal.cell(cid)
    assert [(j.job_id, j.state) for j in merged.jobs] == [("77", "COMPLETED")]
    assert [c.name for c in merged.check_results] == ["counts"]


def test_addendum_suffix_is_checked(journal: Journal) -> None:
    cid = journal.allocate("c")
    with pytest.raises(JournalError):
        journal.add_addendum(cid, "../escape", {"kind": "job"})
    with pytest.raises(JournalError):
        journal.add_addendum(cid, "job-1", {"kind": "unknown"})


def test_notes_need_their_reasons(journal: Journal) -> None:
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid, status="ok"))
    with pytest.raises(JournalError, match="reverses_if"):
        journal.add_note("decision", "use K562 essential", because=(f"demo#{cid}",))
    with pytest.raises(JournalError, match="because"):
        journal.add_note("decision", "use K562 essential", reverses_if="genome-wide fits")
    with pytest.raises(JournalError, match="not in this journal"):
        journal.add_note("finding", "3 of 300 genes", because=("demo#c0099",))
    note = journal.add_note("decision", "use K562 essential", because=(f"demo#{cid}",),
                            reverses_if="the genome-wide set fits in memory", actor=Actor(kind="chat", client="codex"))
    assert note.ref == "demo#n0001" and note.actor.client == "codex"
    assert journal.add_note("error", "I misread the header").kind == "error"


def test_entries_are_ordered_and_filtered_by_time(journal: Journal) -> None:
    first = journal.allocate("c")
    journal.write_cell(cell(journal, first, status="ok"))
    marker = journal.now()
    second = journal.allocate("c")
    journal.write_cell(cell(journal, second, status="ok"))
    journal.add_note("note", "halfway")
    assert [e.ref for e in journal.entries()] == ["demo#c0001", "demo#c0002", "demo#n0001"]
    assert [e.ref for e in journal.entries(since=marker)] == ["demo#c0002", "demo#n0001"]
    assert [e.ref for e in journal.entries(kinds=("note",))] == ["demo#n0001"]


def test_refs() -> None:
    assert parse_ref("ifn/sub#c0007") == ("ifn/sub", "c0007")
    for bad in ("nohash", "p#x0001", "p#c1", "#c0001"):
        with pytest.raises(JournalError):
            parse_ref(bad)


def test_two_checks_of_the_same_kind_both_count(journal: Journal) -> None:
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid, status="ok",
                            check_results=(CheckResult(name="file", status="fail", message="a.csv"),)))
    journal.add_addendum(cid, "check-9-0", {"kind": "check", "check": {"name": "file", "status": "pass"}})
    assert [(c.name, c.status) for c in journal.cell(cid).check_results] == [("file", "fail"), ("file", "pass")]


def test_a_jobs_own_report_wins_over_the_watchdogs_guess(journal: Journal) -> None:
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid, status="ok", jobs=(JobRef(job_id="5", state="PENDING"),)))
    journal.add_addendum(cid, "jobend-5", {"kind": "job", "source": "watchdog", "job": {"job_id": "5", "state": "ENDED"}})
    journal.add_addendum(cid, "job-5", {"kind": "job", "job": {"job_id": "5", "state": "COMPLETED", "exit_code": 0}})
    assert journal.cell(cid).jobs[0].state == "COMPLETED"


def test_since_sees_cells_that_changed_later(journal: Journal) -> None:
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid, status="running"))
    mark = journal.now()
    journal.add_note("note", "made after the mark")
    journal.add_addendum(cid, "job-3", {"kind": "job", "job": {"job_id": "3", "state": "COMPLETED"}})
    assert [e.ref for e in journal.entries(since=mark)] == ["demo#n0001", "demo#c0001"]  # by time of change
    later = journal.changes(since=mark)[-1][0]
    assert journal.entries(since=later) == []


def test_reading_on_from_since_misses_nothing(journal: Journal) -> None:
    mark = journal.now()
    for i in range(5):
        journal.add_note("note", f"note {i}")
    first = journal.changes(since=mark, limit=2)
    second = journal.changes(since=first[-1][0], limit=2)
    third = journal.changes(since=second[-1][0], limit=2)
    texts = [e.text for _, e in first + second + third]
    assert texts == ["note 0", "note 1", "note 2", "note 3", "note 4"]
