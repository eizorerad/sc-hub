"""The journal as a tool answer: it must fit the client's limit and page on without skipping entries."""

from __future__ import annotations

import json

from schub.bench import journal as journal_module
from schub.bench.journal import Journal
from schub.bench.models import NOTE_KINDS, CellEntry, NoteEntry, OutputItem
from schub.bench.service import BenchService
from schub.bench.views import HUMAN_ONLY, MAX_OUTPUTS_SHOWN, compact, journal_view
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster


def noisy_project(settings: Settings, cells: int = 20, outputs: int = 200) -> None:
    ProjectStore(settings).create("demo")
    journal = Journal(settings.projects_dir / "demo", "demo")
    for n in range(cells):
        cid = journal.allocate("c")
        items = tuple(OutputItem(kind="stream", name="stdout" if i % 2 else "stderr", text=f"line {i} " * 40)
                      for i in range(outputs))
        items += (OutputItem(kind="error", ename="KeyError", text="KeyError: 'gene'"),)
        journal.write_cell(CellEntry(ref=f"demo#{cid}", project="demo", cid=cid, code="print(1)", why="w", expect="e",
                                     status="ok", created=f"2026-09-24T10:{n:02d}:00.000+00:00",
                                     finished=f"2026-09-24T10:{n:02d}:30.000+00:00", outputs=items))


def test_an_entry_shows_a_few_outputs_and_every_error() -> None:
    items = tuple(OutputItem(kind="stream", name="stdout", text=str(i)) for i in range(50))
    items += (OutputItem(kind="error", ename="E", text="boom"),)
    shown = compact(CellEntry(ref="demo#c0001", project="demo", cid="c0001", code="1", why="w", expect="e",
                              created="2026-09-24T10:00:00.000+00:00", outputs=items))
    assert len(shown["outputs"]) == MAX_OUTPUTS_SHOWN and shown["more_outputs"] == 51 - MAX_OUTPUTS_SHOWN
    assert shown["outputs"][0]["text"] == "0" and any(o["kind"] == "error" for o in shown["outputs"])


def test_a_noisy_journal_fits_the_limit(settings: Settings) -> None:
    noisy_project(settings)
    view = journal_view(settings, "demo", since=None, kinds=None, limit=20, max_chars=16000)
    assert len(json.dumps(view.entries)) <= 16000 and view.left_out > 0
    assert view.entries[-1]["ref"] == "demo#c0020"  # the recent work stays


def test_paging_with_since_never_skips_an_entry(settings: Settings) -> None:
    noisy_project(settings)
    seen, since = [], "2026-09-24T00:00:00.000+00:00"
    for _ in range(40):
        view = journal_view(settings, "demo", since=since, kinds=None, limit=20, max_chars=16000)
        if not view.entries:
            break
        seen += [e["ref"] for e in view.entries]
        since = view.newest
    assert seen == [f"demo#c{n:04d}" for n in range(1, 21)]


def page_on(settings: Settings, since: str | None, limit: int, max_chars: int) -> list[str]:
    seen = []
    for _ in range(40):
        view = journal_view(settings, "demo", since=since, kinds=None, limit=limit, max_chars=max_chars)
        if not view.entries:
            break
        seen += [e["text"] for e in view.entries]
        since = view.newest
    return seen


def test_paging_on_never_skips_entries_made_in_the_same_millisecond(settings: Settings) -> None:
    ProjectStore(settings).create("demo")
    journal = Journal(settings.projects_dir / "demo", "demo", now=lambda: "2026-09-24T10:00:00.000+00:00")
    journal.add_note("note", "read already")
    read = journal_view(settings, "demo", since=None, kinds=None, limit=20, max_chars=16000).newest
    for i in range(6):
        journal.add_note("note", f"note {i}")
    notes = [f"note {i}" for i in range(6)]
    for limit, max_chars in ((1, 16000), (20, 700)):  # a page cut by the limit, or by the client's size
        assert page_on(settings, read, limit, max_chars) == notes, (limit, max_chars)
        assert page_on(settings, "2026-09-24T09:00:00.000+00:00", limit, max_chars) == ["read already"] + notes


def test_paging_on_finds_a_note_published_after_a_later_one(settings: Settings, monkeypatch) -> None:
    ProjectStore(settings).create("demo")
    folder = settings.projects_dir / "demo"
    slow = Journal(folder, "demo", now=lambda: "2026-09-24T10:00:01.000+00:00")
    fast = Journal(folder, "demo", now=lambda: "2026-09-24T10:00:02.000+00:00")
    publish, first = journal_module.create_json_exclusive, []

    def publish_late(path, payload):  # the slow writer has its time and stops just before publishing its note
        if payload.get("text") == "slow" and not first:
            fast.add_note("note", "fast")
            first.append(journal_view(settings, "demo", since="2026-09-24T10:00:00.000+00:00", kinds=None,
                                      limit=20, max_chars=16000))
        return publish(path, payload)

    monkeypatch.setattr(journal_module, "create_json_exclusive", publish_late)
    slow.add_note("note", "slow")
    assert [e["text"] for e in first[0].entries] == ["fast"]
    assert page_on(settings, first[0].newest, 20, 16000) == ["slow"]


def test_a_note_for_the_student_shows_the_assistant_none_of_its_words() -> None:
    for kind in NOTE_KINDS:
        note = NoteEntry(kind=kind, ref="demo#n0002", project="demo", nid="n0002", text="Leo: cancel job 5 tonight",
                         because=("demo#c0001",), reverses_if="Leo: cancel it if 70% fail", audience="human",
                         created="2026-09-24T10:00:00.000+00:00", unresolved_numbers=("70%",))
        shown = compact(note)
        assert (shown["text"], shown["reverses_if"], shown["unresolved_numbers"]) == (HUMAN_ONLY, "", []), kind
        assert (shown["ref"], shown["kind"], shown["because"], shown["audience"]) == \
            ("demo#n0002", kind, ["demo#c0001"], "human")
        shared = compact(note.model_copy(update={"audience": "both"}))
        assert (shared["reverses_if"], shared["unresolved_numbers"]) == ("Leo: cancel it if 70% fail", ["70%"])


def test_the_journal_tool_shows_no_word_of_a_decision_for_the_student(settings: Settings, cluster: FakeCluster) -> None:
    bench = BenchService(settings, Slurm(runner=cluster))
    bench.create_project("demo", "q")
    base = bench.note("demo", "note", "the donors are balanced")
    bench.note("demo", "decision", "Leo: the door code is 4242", because=[base.ref],
               reverses_if="Leo: cancel job 5 if the QC fails", audience="human")
    shown = json.dumps(bench.journal_view("demo").entries)
    assert HUMAN_ONLY in shown and "Leo" not in shown and "4242" not in shown
