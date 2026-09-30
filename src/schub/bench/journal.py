"""The project journal: one file per record, append-only.

    projects/<p>/journal/
      ids/c0007                 id markers (allocation by exclusive create)
      cells/c0007.json          a cell; written only by the runner, frozen once final
      cells/c0007.job-812.json  addenda (a job ended, a check ran); created once, never changed
      cells/c0007/              artifacts (figures)
      notes/n0003.json          decisions, findings, errors, registrations...
      changes/000042.json       the 42nd change, {"id": "c0007"}: numbered once it can be read

Ids are handed out by the server when a record is appended (VCC2026 once gave two
findings the same number, F48/F49). A decision must name the cells it rests on and
the condition that would reverse it; a finding must name its cells.

Reading on from an earlier read goes by the change numbers, not by time. A time is
taken before its record is published: records made in the same millisecond share
one, and a writer that stalls in between publishes a record older than those a
reader has seen already. A change gets its number after it is published, and number
n only once n-1 exists (exclusive create), so a reader that has every change up to n
and reads on from there misses none, whatever their times. (A writer that dies or
fails between the two leaves a change that only a read without `since` shows.)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import ValidationError

from .clock import Clock, stamp
from .fsio import create_json_exclusive, read_json, write_json_atomic
from .models import (
    CID_PATTERN, FINAL_STATUSES, MAX_TEXT_CHARS, NID_PATTERN, NOTE_KINDS, Actor, CellEntry, CheckResult, Download,
    FileChange, JobRef, NoteEntry,
)

REF = re.compile(r"^(?P<project>[a-z0-9][a-z0-9_/-]*)#(?P<id>[cn]\d{4,})$")
SUFFIX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
# where a read ended (see Journal.page): "#42", after change 42; while an older client's time is read on
# from, "#42,<time>,<ref>", after that record, then on from change 42
PLACE = re.compile(r"^#(?P<number>\d+)(?:,(?P<changed>[^,]+),(?P<ref>[^,]+))?$")
ADDENDUM_KINDS = ("job", "check", "download", "files")
NEEDS_BECAUSE = ("decision", "finding", "verdict")


class JournalError(ValueError):
    pass


class FinalEntryError(JournalError):
    """The entry is final already (e.g. the watchdog closed it while the cell was still running)."""


def parse_ref(ref: str) -> tuple[str, str]:
    match = REF.fullmatch(ref.strip())
    if match is None:
        raise JournalError(f"'{ref}' is not a journal reference like 'project#c0007'")
    return match["project"], match["id"]


def _number(record_id: str) -> int:
    return int(record_id[1:])


def check_cid(cid: str) -> str:
    if not re.fullmatch(CID_PATTERN, cid):
        raise JournalError(f"'{cid}' is not a cell id like c0007")
    return cid


def check_nid(nid: str) -> str:
    if not re.fullmatch(NID_PATTERN, nid):
        raise JournalError(f"'{nid}' is not a note id like n0003")
    return nid


class Journal:
    def __init__(self, project_dir: Path, project: str, now: Clock = stamp) -> None:
        self.project = project
        self.folder = project_dir / "journal"
        self.now = now

    @property
    def cells_dir(self) -> Path:
        return self.folder / "cells"

    @property
    def notes_dir(self) -> Path:
        return self.folder / "notes"

    @property
    def changes_dir(self) -> Path:
        return self.folder / "changes"

    def ref(self, record_id: str) -> str:
        return f"{self.project}#{record_id}"

    def artifacts_dir(self, cid: str) -> Path:
        return self.cells_dir / check_cid(cid)

    # ---- ids ------------------------------------------------------------------

    def allocate(self, prefix: str) -> str:
        if prefix not in ("c", "n"):
            raise JournalError(f"unknown id prefix {prefix!r}")
        ids = self.folder / "ids"
        ids.mkdir(parents=True, exist_ok=True)
        taken = [_number(p.name) for p in ids.iterdir() if p.name[:1] == prefix and p.name[1:].isdigit()]
        n = max(taken, default=0) + 1
        while True:
            record_id = f"{prefix}{n:04d}"
            try:
                os.close(os.open(ids / record_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                return record_id
            except FileExistsError:
                n += 1

    def _number_change(self, record_id: str) -> None:
        """Number a change that can be read now (see the module's docstring)."""
        n = self._last_change() + 1
        while not create_json_exclusive(self.changes_dir / f"{n:06d}.json", {"id": record_id}):
            n += 1

    def _last_change(self) -> int:
        """The newest change number up to which every number exists (see _listed_change)."""
        listed = dict(self._change_files())
        n = 0
        while self._listed_change(listed, n + 1) is not None:
            n += 1
        return n

    def _change_files(self) -> list[tuple[int, Path]]:
        if not self.changes_dir.is_dir():
            return []
        return sorted((int(p.stem), p) for p in self.changes_dir.glob("*.json") if p.stem.isdigit())

    def _listed_change(self, listed: dict[int, Path], n: int) -> Path | None:
        """Change n's file. Numbers go up by one, each only after the one before, so a number missing from a
        directory listing while a later one is in it is a listing gap (readdir is not atomic while files are being
        created): it is looked up, never believed missing, so a read does not end before it."""
        if n in listed:
            return listed[n]
        path = self.changes_dir / f"{n:06d}.json"
        return path if path.exists() else None

    # ---- cells ----------------------------------------------------------------

    def write_cell(self, entry: CellEntry) -> None:
        """Only the runner calls this. A final entry is never rewritten."""
        path = self.cells_dir / f"{check_cid(entry.cid)}.json"
        current = read_json(path)
        if current is not None and current.get("status") in FINAL_STATUSES:
            raise FinalEntryError(f"{entry.ref} is final ({current['status']}); add an addendum instead")
        data = entry.model_dump(mode="json")
        write_json_atomic(path, data)
        if current is None or any(current.get(key) != data[key] for key in ("status", "started", "finished")):
            self._number_change(entry.cid)  # outputs written while it runs are not a change

    def raw_cell(self, cid: str) -> CellEntry | None:
        data = read_json(self.cells_dir / f"{check_cid(cid)}.json")
        try:
            return CellEntry.model_validate(data) if data is not None else None
        except ValidationError:
            return None

    def cell(self, cid: str) -> CellEntry | None:
        base = self.raw_cell(cid)
        if base is None:
            return None
        return _merge(base, self._addenda(cid))

    def _addenda(self, cid: str) -> list[dict[str, Any]]:
        if not self.cells_dir.is_dir():
            return []
        found = []
        for path in sorted(self.cells_dir.glob(f"{cid}.*.json")):
            data = read_json(path)
            if data is not None:
                found.append(data)
        return found

    def add_addendum(self, cid: str, suffix: str, payload: dict[str, Any]) -> bool:
        """Attach something that happened later (a job ended, a check ran). Once per suffix."""
        check_cid(cid)
        if not SUFFIX.fullmatch(suffix):
            raise JournalError(f"bad addendum name {suffix!r}")
        if payload.get("kind") not in ADDENDUM_KINDS:
            raise JournalError(f"addendum kind must be one of {ADDENDUM_KINDS}")
        stamped = {**payload, "added": self.now()}
        if not create_json_exclusive(self.cells_dir / f"{cid}.{suffix}.json", stamped):
            return False
        self._number_change(cid)
        return True

    # ---- notes ----------------------------------------------------------------

    def add_note(
        self,
        kind: str,
        text: str,
        because: Sequence[str] = (),
        reverses_if: str = "",
        verdict: str | None = None,
        audience: str = "both",
        actor: Actor | None = None,
        unresolved_numbers: Sequence[str] = (),
    ) -> NoteEntry:
        text, reverses_if = text.strip(), reverses_if.strip()
        self._check_note(kind, text, because, reverses_if, verdict)
        nid = self.allocate("n")
        note = NoteEntry(
            kind=kind, ref=self.ref(nid), project=self.project, nid=nid, text=text,
            because=tuple(because), reverses_if=reverses_if, verdict=verdict, audience=audience,
            actor=actor or Actor(), created=self.now(), unresolved_numbers=tuple(unresolved_numbers),
        )
        if not create_json_exclusive(self.notes_dir / f"{nid}.json", note.model_dump(mode="json")):
            raise JournalError(f"note {nid} already exists")
        self._number_change(nid)
        return note

    def _check_note(self, kind: str, text: str, because: Sequence[str], reverses_if: str, verdict: str | None) -> None:
        if kind not in NOTE_KINDS:
            raise JournalError(f"note kind must be one of {', '.join(NOTE_KINDS)}")
        if not text or len(text) > MAX_TEXT_CHARS:
            raise JournalError(f"a note needs text (at most {MAX_TEXT_CHARS} characters)")
        if kind in NEEDS_BECAUSE and not because:
            raise JournalError(f"a {kind} needs `because`: the cells it rests on")
        if kind == "decision" and not reverses_if:
            raise JournalError("a decision needs `reverses_if`: what result would reverse it")
        if kind == "verdict" and verdict is None:
            raise JournalError("a verdict needs `verdict`: registered, descriptive or invalid")
        for ref in because:
            project, record_id = parse_ref(ref)
            if project != self.project or not self.exists(record_id):
                raise JournalError(f"{ref} is not in this journal")

    def evidence(self, refs: Sequence[str]) -> list[str]:
        """The text of the cited records: cell outputs and check messages, note texts."""
        found: list[str] = []
        for ref in refs:
            project, record_id = parse_ref(ref)
            if project != self.project:
                continue
            if record_id.startswith("c"):
                cell = self.cell(record_id)
                if cell is not None:
                    found += [o.text for o in cell.outputs] + [c.message for c in cell.check_results]
            else:
                note = self.note(record_id)
                found += [note.text] if note is not None else []
        return found

    def exists(self, record_id: str) -> bool:
        if not (re.fullmatch(CID_PATTERN, record_id) or re.fullmatch(NID_PATTERN, record_id)):
            return False
        folder = self.cells_dir if record_id.startswith("c") else self.notes_dir
        return (folder / f"{record_id}.json").is_file()

    def note(self, nid: str) -> NoteEntry | None:
        data = read_json(self.notes_dir / f"{check_nid(nid)}.json")
        try:
            return NoteEntry.model_validate(data) if data is not None else None
        except ValidationError:
            return None

    # ---- reading ----------------------------------------------------------------

    def entries(
        self, since: str | None = None, kinds: Iterable[str] | None = None, limit: int | None = None,
    ) -> list[CellEntry | NoteEntry]:
        """Without `since`: the newest `limit` records in the order they were made. With `since` (the
        place where an earlier read ended, see page(), or a time): records made or changed after it (a
        cell that finished, a job that reported), oldest change first, so reading on misses nothing."""
        return [entry for _, entry in self.changes(since, kinds, limit)]

    def changes(
        self, since: str | None = None, kinds: Iterable[str] | None = None, limit: int | None = None,
    ) -> list[tuple[str, CellEntry | NoteEntry]]:
        """(the place to read on from after the record, record) pairs; see entries()."""
        return self.page(since, kinds, limit)[0]

    def page(
        self, since: str | None = None, kinds: Iterable[str] | None = None, limit: int | None = None,
    ) -> tuple[list[tuple[str, CellEntry | NoteEntry]], str]:
        """changes(), and the place to read on from after all of them (also when there are none). Places
        go on by change number; a time (an older client's place) is read on from by time, and the places
        after it go on by number from the last change there was when that began."""
        wanted = set(kinds) if kinds is not None else None
        place = PLACE.fullmatch(since) if since is not None else None
        if place is not None and place["changed"] is None:
            return self._numbered_after(int(place["number"]), wanted, limit)
        if place is not None:
            return self._changed_after((place["changed"], place["ref"]), int(place["number"]), wanted, limit)
        last = self._last_change()  # before the records are read: a change numbered later is new next time
        if since is not None:
            return self._changed_after((since, ""), last, wanted, limit)
        found = sorted(self._changed(wanted), key=lambda pair: (pair[1].created, pair[1].ref))
        return [(f"#{last}", entry) for _, entry in (found[-limit:] if limit else found)], f"#{last}"

    def _changed(self, wanted: set[str] | None) -> list[tuple[str, CellEntry | NoteEntry]]:
        """(time of the last change, record) pairs of every record."""
        addenda = self._all_addenda()
        found: list[tuple[str, CellEntry | NoteEntry]] = []
        for record_id in self._ids():
            if record_id.startswith("c"):
                base = self.raw_cell(record_id)
                extra = addenda.get(record_id, [])
                entry = _merge(base, extra) if base is not None else None
                changed = max([base.created, base.started or "", base.finished or ""]
                              + [str(a.get("added", "")) for a in extra]) if base is not None else ""
            else:
                entry = self.note(record_id)
                changed = entry.created if entry is not None else ""
            if entry is not None and (wanted is None or entry.kind in wanted):
                found.append((changed, entry))
        return found

    def _changed_after(self, after: tuple[str, str], last: int, wanted: set[str] | None,
                       limit: int | None) -> tuple[list[tuple[str, CellEntry | NoteEntry]], str]:
        """Records changed after `after`, a (time, ref), in that order. Once they are all read, on by number
        from change `last`: what was published meanwhile with an earlier time comes then."""
        found = sorted(((changed, entry) for changed, entry in self._changed(wanted) if (changed, entry.ref) > after),
                       key=lambda pair: (pair[0], pair[1].ref))
        return _cut([(f"#{last},{changed},{entry.ref}", entry) for changed, entry in found], limit, f"#{last}")

    def _numbered_after(self, number: int, wanted: set[str] | None,
                        limit: int | None) -> tuple[list[tuple[str, CellEntry | NoteEntry]], str]:
        """Every record changed after change `number`, once, as it is now, in the order of its last change."""
        last: dict[str, int] = {}
        listed = dict(self._change_files())
        end = number
        while (path := self._listed_change(listed, end + 1)) is not None:
            record_id = str((read_json(path) or {}).get("id", ""))
            if not record_id:
                break  # still being written (where link() is not allowed): the changes after it wait
            end += 1
            last.pop(record_id, None)
            last[record_id] = end
        found = []
        for record_id, n in last.items():
            entry = self.cell(record_id) if re.fullmatch(CID_PATTERN, record_id) else \
                self.note(record_id) if re.fullmatch(NID_PATTERN, record_id) else None
            if entry is not None and (wanted is None or entry.kind in wanted):
                found.append((f"#{n}", entry))
        return _cut(found, limit, f"#{end}")

    def _all_addenda(self) -> dict[str, list[dict[str, Any]]]:
        """Every addendum, by cell, from one listing of the folder."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        if not self.cells_dir.is_dir():
            return grouped
        for path in sorted(self.cells_dir.glob("c*.*.json")):
            cid = path.name.partition(".")[0]
            data = read_json(path)
            if data is not None and re.fullmatch(CID_PATTERN, cid):
                grouped.setdefault(cid, []).append(data)
        return grouped

    def latest_cells(self, count: int) -> list[CellEntry]:
        """The newest `count` cells, oldest first, without reading the whole journal."""
        ids = [i for i in self._ids() if i.startswith("c")][-count:]
        return [c for c in (self.raw_cell(i) for i in ids) if c is not None]

    def _ids(self) -> list[str]:
        ids: list[str] = []
        for folder in (self.cells_dir, self.notes_dir):
            if folder.is_dir():
                ids += [p.stem for p in folder.glob("*.json")
                        if re.fullmatch(CID_PATTERN, p.stem) or re.fullmatch(NID_PATTERN, p.stem)]
        return sorted(ids, key=lambda i: (i[0], _number(i)))


def _cut(found: list[tuple[str, CellEntry | NoteEntry]], limit: int | None,
         end: str) -> tuple[list[tuple[str, CellEntry | NoteEntry]], str]:
    """The first `limit` pairs and the place to read on from after them: after the last one given, or `end`
    (after every change read) when none is left out."""
    if limit and len(found) > limit:
        return found[:limit], found[limit - 1][0]
    if not found:
        return [], end
    return found[:-1] + [(end, found[-1][1])], end


def _merge(base: CellEntry, addenda: list[dict[str, Any]]) -> CellEntry:
    jobs = {j.job_id: j for j in base.jobs}
    checks = list(base.check_results)  # two checks of the same kind are two results, never one
    downloads = list(base.downloads)
    files = list(base.files)
    # The watchdog's guess about a job that ended silently comes first; the job's own report wins.
    for addendum in sorted(addenda, key=lambda a: a.get("source") != "watchdog"):
        try:
            kind = addendum.get("kind")
            if kind == "job":
                job = JobRef.model_validate(addendum["job"])
                known = jobs.get(job.job_id)
                jobs[job.job_id] = JobRef.model_validate({**known.model_dump(), **job.model_dump(exclude_unset=True)}) \
                    if known else job
            elif kind == "check":
                checks.append(CheckResult.model_validate(addendum["check"]))
            elif kind == "download":
                downloads.append(Download.model_validate(addendum["download"]))
            elif kind == "files":
                files += [FileChange.model_validate(f) for f in addendum["files"]]
        except (KeyError, TypeError, ValidationError):
            continue  # a malformed addendum is skipped, never fatal to reading the journal
    return base.model_copy(update={
        "jobs": tuple(jobs.values()), "check_results": tuple(checks),
        "downloads": tuple(downloads), "files": tuple(files),
    })
