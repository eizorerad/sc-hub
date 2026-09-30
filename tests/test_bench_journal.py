from __future__ import annotations

import multiprocessing
import os
import re
from pathlib import Path

import pytest

from schub.bench import journal as journal_module
from schub.bench.journal import Journal, JournalError, parse_ref
from schub.bench.models import Actor, CellEntry, CheckResult, JobRef, OutputItem


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


def read_on(journal: Journal, since: str | None, limit: int = 1) -> list[str]:
    seen = []
    for _ in range(20):
        page = journal.changes(since=since, limit=limit)
        if not page:
            break
        seen += [e.text for _, e in page]
        since = page[-1][0]
    return seen


def test_reading_on_misses_no_record_made_in_the_same_millisecond(tmp_path: Path) -> None:
    writer = Journal(tmp_path / "projects" / "demo", "demo", now=lambda: "2026-09-24T10:00:00.000+00:00")
    for i in range(5):
        writer.add_note("note", f"note {i}")
    reader = Journal(tmp_path / "projects" / "demo", "demo")  # (a reader's clock runs: where it began is a time)
    assert read_on(reader, "2026-09-24T09:00:00.000+00:00") == [f"note {i}" for i in range(5)]


def stall_before_publishing(monkeypatch, stalled, meanwhile) -> dict:
    """The writer of the record that `stalled` picks has taken its time and stops right before it publishes:
    `meanwhile` runs then (others write, a reader reads), and its answer is kept."""
    publish, kept = journal_module.create_json_exclusive, {}

    def publish_late(path: Path, payload: dict) -> bool:
        if stalled(payload) and not kept:
            kept["answer"] = meanwhile()
        return publish(path, payload)

    monkeypatch.setattr(journal_module, "create_json_exclusive", publish_late)
    return kept


def test_reading_on_finds_a_note_published_after_a_later_one(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "projects" / "demo"
    slow = Journal(folder, "demo", now=lambda: "2026-09-24T10:00:01.000+00:00")
    fast = Journal(folder, "demo", now=lambda: "2026-09-24T10:00:02.000+00:00")
    reader = Journal(folder, "demo")

    def meanwhile() -> list:
        fast.add_note("note", "fast")
        return reader.changes(since="2026-09-24T10:00:00.000+00:00")

    kept = stall_before_publishing(monkeypatch, lambda payload: payload.get("text") == "slow", meanwhile)
    slow.add_note("note", "slow")
    first = kept["answer"]
    assert [e.text for _, e in first] == ["fast"]
    assert [e.text for _, e in reader.changes(since=first[-1][0])] == ["slow"]


def test_reading_on_finds_an_addendum_published_after_a_later_one(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "projects" / "demo"
    journal = Journal(folder, "demo", now=Ticks())
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid, status="ok", jobs=(JobRef(job_id="5", state="PENDING"),)))
    report = Journal(folder, "demo", now=lambda: "2026-09-24T11:00:01.000+00:00")
    watchdog = Journal(folder, "demo", now=lambda: "2026-09-24T11:00:02.000+00:00")

    def meanwhile() -> list:
        watchdog.add_addendum(cid, "jobend-5", {"kind": "job", "source": "watchdog",
                                                "job": {"job_id": "5", "state": "ENDED"}})
        return journal.changes(since="2026-09-24T11:00:00.000+00:00")

    kept = stall_before_publishing(monkeypatch, lambda p: p.get("kind") == "job" and "source" not in p, meanwhile)
    report.add_addendum(cid, "job-5", {"kind": "job", "job": {"job_id": "5", "state": "COMPLETED", "exit_code": 0}})
    first = kept["answer"]
    assert [e.jobs[0].state for _, e in first] == ["ENDED"]
    assert [e.jobs[0].state for _, e in journal.changes(since=first[-1][0])] == ["COMPLETED"]


def test_outputs_written_while_a_cell_runs_are_not_a_change(journal: Journal) -> None:
    cid = journal.allocate("c")
    running = cell(journal, cid, status="running", started=journal.now())
    journal.write_cell(running)
    since = journal.changes(since=None)[-1][0]
    journal.write_cell(running.model_copy(update={"outputs": (OutputItem(kind="stream", text="epoch 1"),)}))
    assert journal.changes(since=since) == []
    journal.write_cell(running.model_copy(update={"status": "ok", "finished": journal.now()}))
    assert [e.status for _, e in journal.changes(since=since)] == ["ok"]


def number_of(place: str) -> int:
    """The change number in a place: '#5' or '#5@<time>' (the time is where the look at old code's records goes on)."""
    return int(re.fullmatch(r"#(\d+)(?:@.*)?", place)[1])


def hide_from_listing(monkeypatch, journal: Journal, *numbers: int) -> None:
    """A directory listing made while files are being created can skip one that is there (readdir is not atomic)."""
    listed = journal._change_files
    monkeypatch.setattr(journal, "_change_files", lambda: [(n, p) for n, p in listed() if n not in numbers])


def test_a_listing_that_skips_a_change_does_not_make_the_reader_skip_it_for_good(tmp_path: Path, monkeypatch) -> None:
    """Change numbers go up by one, each only after the one before, so a number the listing shows without its
    predecessor is a listing gap: the reader looks the missing file up before it ends its read there."""
    journal = Journal(tmp_path / "projects" / "demo", "demo", now=Ticks())
    for i in range(5):
        journal.add_note("note", f"note {i}")
    hide_from_listing(monkeypatch, journal, 3)
    changes, after = journal.page(since="#0")
    assert [e.text for _, e in changes] == [f"note {i}" for i in range(5)] and number_of(after) == 5
    assert [e.text for _, e in journal.page(since="#2")[0]] == ["note 2", "note 3", "note 4"]
    assert number_of(journal.page(since=None)[1]) == 5  # the place after a full read is not the last number listed
    journal.add_note("note", "note 5")
    assert number_of(journal.page(since="#5")[1]) == 6
    assert sorted(p.name for p in journal.changes_dir.iterdir())[-1] == "000006.json"


def test_a_number_that_is_really_missing_holds_the_read_there_until_it_is_filled(tmp_path: Path) -> None:
    """Not something the numbering can do; if a file is lost, the read waits at the hole and the next change fills it."""
    journal = Journal(tmp_path / "projects" / "demo", "demo", now=Ticks())
    for i in range(4):
        journal.add_note("note", f"note {i}")
    (journal.changes_dir / "000003.json").unlink()
    changes, after = journal.page(since="#0")
    assert [e.text for _, e in changes] == ["note 0", "note 1"] and number_of(after) == 2
    journal.add_note("note", "note 4")  # takes number 3
    assert (journal.changes_dir / "000003.json").exists()
    assert [e.text for _, e in journal.page(since="#2")[0]] == ["note 4", "note 3"]  # (note 2's number was lost)


class OldCodeJournal(Journal):
    """The journal code from before the change numbers: it writes every record and never numbers it (a workbench,
    a kernel or a job started before the upgrade keeps it until it ends: up to a day)."""

    def _number_change(self, record_id: str, changed: str) -> bool:
        return True


def test_records_of_code_from_before_the_numbers_are_found_while_it_can_still_be_running(tmp_path: Path) -> None:
    """Found in review: after an upgrade, running workbenches write without numbers, and a read on from a number
    skipped all of it (54 of 108 records in the review's run, every old note, for good)."""
    folder, now = tmp_path / "projects" / "demo", Ticks()
    new, old = Journal(folder, "demo", now=now), OldCodeJournal(folder, "demo", now=now)
    new.add_note("note", "new one")
    cid = new.allocate("c")
    new.write_cell(cell(new, cid, status="running", started=new.now()))
    _, place = new.page(since=None)
    old.add_note("note", "old one")  # the old worker finishes the cell the new code started, and writes a note
    old.write_cell(cell(old, cid, status="ok", started=old.now(), finished=old.now()))
    new.add_note("note", "new two")
    changes, after = new.page(since=place)
    seen = [(e.kind, e.text if e.kind == "note" else e.status) for _, e in changes]
    assert ("note", "new two") in seen and ("note", "old one") in seen and ("cell", "ok") in seen and len(seen) == 3
    assert new.page(since=after)[0] == []  # and each only once: the place has moved past them


def test_the_look_at_old_codes_records_ends_when_it_cannot_be_running_any_more(tmp_path: Path, monkeypatch) -> None:
    folder, now = tmp_path / "projects" / "demo", Ticks()
    new, old = Journal(folder, "demo", now=now), OldCodeJournal(folder, "demo", now=now)
    new.add_note("note", "new one")
    _, place = new.page(since=None)
    old.add_note("note", "old one")
    assert [e.text for _, e in new.page(since=place)[0]] == ["old one"]
    monkeypatch.setattr(journal_module, "LEGACY_S", -1.0)  # the first number is older than any old process can be
    _, late = new.page(since=None)
    assert "@" not in late  # nothing to go on looking for: a plain number from now on
    old.add_note("note", "too late")
    assert new.page(since=late)[0] == []
    assert "too late" in [e.text for e in new.entries()]  # a read without `since` always has everything


def test_a_numbering_that_fails_does_not_fail_the_write_and_can_be_done_again(tmp_path: Path, monkeypatch) -> None:
    """Found in review: the record was on disk but the call raised, so the agent's retry made a duplicate note, and
    a job's recovery found 'already exists' and never numbered it."""
    journal = Journal(tmp_path / "projects" / "demo", "demo", now=Ticks())
    monkeypatch.setattr(journal_module, "NUMBERING_PAUSE_S", 0.0)
    real, failing = journal_module.create_json_exclusive, {"on": True}

    def refuse_numbers(path: Path, payload: dict, *args):
        if failing["on"] and path.parent.name == "changes":
            raise OSError(5, "Input/output error")
        return real(path, payload, *args)

    monkeypatch.setattr(journal_module, "create_json_exclusive", refuse_numbers)
    note = journal.add_note("note", "written all the same")
    cid = journal.allocate("c")
    journal.write_cell(cell(journal, cid, status="ok", started=journal.now(), finished=journal.now()))
    assert journal.add_addendum(cid, "job-5", {"kind": "job", "job": {"job_id": "5", "state": "COMPLETED"}}) is True
    assert [e.text for e in journal.entries(kinds=("note",))] == ["written all the same"] and note.ref.endswith("n0001")
    assert not journal.changes_dir.is_dir() or not list(journal.changes_dir.glob("*.json"))
    # a reader that went on from a number still finds them: they are recent, and old code may be running
    assert {e.ref for _, e in journal.page(since="#0@2026-09-24T09:00:00.000+00:00")[0]} == {note.ref, f"demo#{cid}"}
    assert journal.note_change(cid) is False  # the file server still refuses
    failing["on"] = False
    assert journal.note_change("n0001") is True and journal.note_change(cid) is True
    assert [e.ref for _, e in journal.page(since="#0")[0]] == [note.ref, f"demo#{cid}"]


def test_an_empty_change_file_that_nobody_is_writing_any_more_is_skipped(tmp_path: Path) -> None:
    """Where link() is refused a writer killed mid-write leaves an empty file, and the writers after it number on:
    the reads must not stop at it for good."""
    journal = Journal(tmp_path / "projects" / "demo", "demo", now=Ticks())
    for i in range(4):
        journal.add_note("note", f"note {i}")
    lost = journal.changes_dir / "000002.json"
    lost.write_text("")
    changes, after = journal.page(since="#0")
    assert [e.text for _, e in changes] == ["note 0"] and number_of(after) == 1  # still being written: it waits
    old = journal_module.time.time() - 10 * journal_module.LOST_CHANGE_S
    os.utime(lost, (old, old))
    changes, after = journal.page(since="#0")
    assert [e.text for _, e in changes] == ["note 0", "note 2", "note 3"] and number_of(after) == 4


def test_reading_on_reads_only_what_it_needs(tmp_path: Path, monkeypatch) -> None:
    """Found in review: a read from an old place loaded every changed record (each one listing the whole cells
    folder) before the limit applied: 18.9 s for 3,000 cells against 1.1 s for a full read."""
    journal = Journal(tmp_path / "projects" / "demo", "demo", now=Ticks())
    for _ in range(60):
        cid = journal.allocate("c")
        journal.write_cell(cell(journal, cid, status="ok", started=journal.now(), finished=journal.now()))
        journal.add_addendum(cid, "job-1", {"kind": "job", "job": {"job_id": "1", "state": "COMPLETED"}})
    reads = []
    real = journal_module.read_json
    monkeypatch.setattr(journal_module, "read_json", lambda path: reads.append(path) or real(path))
    changes, after = journal.page(since="#0", limit=3)
    assert len(changes) == 3 and len(reads) < 40, len(reads)  # (every cell and addendum file: 180 and more)
    first_three = [e.ref for _, e in changes]
    rest = journal.page(since=changes[-1][0], limit=100)[0]
    # (the third cell's addendum came after the page ended: that cell is read again, as it is now)
    assert {e.ref for _, e in rest} | set(first_three) == {f"demo#c{i:04d}" for i in range(1, 61)}
    assert len(rest) in (57, 58)
