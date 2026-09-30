"""The project journal: one file per record, append-only.

    projects/<p>/journal/
      ids/c0007                 id markers (allocation by exclusive create)
      cells/c0007.json          a cell; written only by the runner, frozen once final
      cells/c0007.job-812.json  addenda (a job ended, a check ran); created once, never changed
      cells/c0007/              artifacts (figures)
      notes/n0003.json          decisions, findings, errors, registrations...
      changes/000042.json       the 42nd change, {"id": "c0007", "changed": <its time>}: numbered once it can be read

Ids are handed out by the server when a record is appended (VCC2026 once gave two
findings the same number, F48/F49). A decision must name the cells it rests on and
the condition that would reverse it; a finding must name its cells.

Reading on from an earlier read goes by the change numbers, not by time. A time is
taken before its record is published: records made in the same millisecond share
one, and a writer that stalls in between publishes a record older than those a
reader has seen already. A change gets its number after it is published, and number
n only once n-1 exists (exclusive create), so a reader that has every change up to n
and reads on from there misses none, whatever their times.

Two things keep that honest where the numbers do not reach. Code from before the
numbers (a workbench, a kernel, a job started before an upgrade, for up to a day)
writes records and never numbers them: while such code can still be running, a read
that goes on from a place also takes the records changed since the time the place
carries ("#42@<time>"), each once. And a writer that fails to number a record, or
dies between the two, does not fail the write: the record is on disk, found by that
same look while it lasts and by every read without `since`; note_change() numbers it
again.
"""

from __future__ import annotations

import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import ValidationError

from .clock import Clock, parse, stamp
from .fsio import create_json_exclusive, read_json, write_json_atomic
from .models import (
    CID_PATTERN, FINAL_STATUSES, MAX_TEXT_CHARS, NID_PATTERN, NOTE_KINDS, Actor, CellEntry, CheckResult, Download,
    FileChange, JobRef, NoteEntry,
)

REF = re.compile(r"^(?P<project>[a-z0-9][a-z0-9_/-]*)#(?P<id>[cn]\d{4,})$")
SUFFIX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
# where a read ended (see Journal.page): "#42", after change 42; "#42@<time>", and also after what old code
# changed before that time; "#42@<time>;<ref>", after that record of it; while an older client's time is read on
# from, "#42,<time>,<ref>", after that record, then on from change 42
PLACE = re.compile(r"^#(?P<number>\d+)(?:@(?P<time>[^;,]+)(?:;(?P<tref>[^;,]+))?|,(?P<changed>[^,]+),(?P<cref>[^,]+))?$")
LEGACY_S = 48 * 3600.0  # code from before the numbers cannot run longer than this after the first number
LOST_CHANGE_S = 60.0  # an empty change file this old is not being written any more
LEGACY_OVERLAP_S = 3.0  # old code takes a record's time before it publishes it: look this far back of a read's start
ANNOUNCED_SLACK_S = 60.0  # numbers do not come in exactly the order of their times (a writer that stalls)
ANNOUNCED_MAX_LOOKBACK = 2000
NUMBERING_TRIES = 3
NUMBERING_PAUSE_S = 0.05
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

    def _number_change(self, record_id: str, changed: str) -> bool:
        """Number a change, made at `changed`, that can be read now (see the module's docstring): the file says
        when the record last changed, so the look at old code's records can tell what a number already brought.
        False if the file server refused every try: the record is written all the same, and is found as the
        module's docstring says."""
        for attempt in range(NUMBERING_TRIES):
            try:
                n = self._last_change() + 1
                payload = {"id": record_id, "changed": changed}
                while not create_json_exclusive(self.changes_dir / f"{n:06d}.json", payload):
                    n += 1
                return True
            except OSError:
                time.sleep(NUMBERING_PAUSE_S * (attempt + 1))
        return False

    def note_change(self, record_id: str) -> bool:
        """Number the last change of a record again (once more is harmless: a read shows a record once, as it is
        now). For whoever finds it was never numbered; False while the file server refuses, or if there is no
        such record."""
        if not (re.fullmatch(CID_PATTERN, record_id) or re.fullmatch(NID_PATTERN, record_id)):
            return False
        pairs = self._changed(None, only=record_id)
        return bool(pairs) and self._number_change(record_id, pairs[0][0])

    def _legacy_open(self) -> bool:
        """Whether code from before the numbers can still be writing here: not longer than LEGACY_S after the
        first number (none yet: it may be all there is)."""
        try:
            return time.time() - (self.changes_dir / "000001.json").stat().st_mtime < LEGACY_S
        except OSError:
            return True

    def _lost(self, path: Path) -> bool:
        """A change file nobody is writing any more (where link() is refused a writer can be killed halfway)."""
        try:
            return time.time() - path.stat().st_mtime > LOST_CHANGE_S
        except OSError:
            return False
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
            # (outputs written while it runs are not a change)
            self._number_change(entry.cid, max(entry.created, entry.started or "", entry.finished or ""))

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
        self._number_change(cid, stamped["added"])
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
        self._number_change(nid, note.created)
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
        began = self.now()  # before anything is read: what changes after it is read again next time, not lost
        place = PLACE.fullmatch(since) if since is not None else None
        if place is not None and place["changed"] is not None:
            return self._changed_after((place["changed"], place["cref"]), int(place["number"]), wanted, limit, began)
        if place is not None:
            after = (place["time"], place["tref"] or "") if place["time"] is not None else None
            return self._numbered_after(int(place["number"]), after, wanted, limit, began)
        last = self._last_change()  # before the records are read: a change numbered later is new next time
        if since is not None:
            return self._changed_after((since, ""), last, wanted, limit, began)
        found = sorted(self._changed(wanted), key=lambda pair: (pair[1].created, pair[1].ref))
        end = self._place(last, began)
        return [(end, entry) for _, entry in (found[-limit:] if limit else found)], end

    def _place(self, number: int, began: str) -> str:
        """The place after change `number` and, while old code can be running, after what it changed before `began`."""
        return f"#{number}@{began}" if self._legacy_open() else f"#{number}"

    def _changed(self, wanted: set[str] | None, only: str | None = None) -> list[tuple[str, CellEntry | NoteEntry]]:
        """(time of the last change, record) pairs of every record (of `only`, if given)."""
        addenda = self._all_addenda() if only is None else None
        found: list[tuple[str, CellEntry | NoteEntry]] = []
        for record_id in self._ids() if only is None else [only]:
            if record_id.startswith("c"):
                base = self.raw_cell(record_id)
                extra = addenda.get(record_id, []) if addenda is not None else self._addenda(record_id)
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
                       limit: int | None, began: str) -> tuple[list[tuple[str, CellEntry | NoteEntry]], str]:
        """Records changed after `after`, a (time, ref), in that order. Once they are all read, on by number
        from change `last`: what was published meanwhile with an earlier time comes then."""
        found = sorted(((changed, entry) for changed, entry in self._changed(wanted) if (changed, entry.ref) > after),
                       key=lambda pair: (pair[0], pair[1].ref))
        return _cut([(f"#{last},{changed},{entry.ref}", entry) for changed, entry in found], limit,
                    self._place(last, began))

    def _numbered_after(self, number: int, after: tuple[str, str] | None, wanted: set[str] | None,
                        limit: int | None, began: str) -> tuple[list[tuple[str, CellEntry | NoteEntry]], str]:
        """Every record changed after change `number`, once, as it is now, in the order of its last change. Then,
        if `after` (a time and a ref) is given and old code can still be running, what that changed after it and
        never numbered, by time. Without a kind filter a page is full once it has `limit` records: the changes
        after that wait for the next page."""
        listed = dict(self._change_files())
        last: dict[str, int] = {}  # record id -> the number of its last change, in the order of those
        end = number
        while wanted is not None or not limit or len(last) < limit:
            path = self._listed_change(listed, end + 1)
            if path is None:
                break
            record_id = str((read_json(path) or {}).get("id", ""))
            if not record_id:
                if not self._lost(path):
                    break  # still being written (where link() is not allowed): the changes after it wait
                end += 1  # a number nobody writes any more
                continue
            end += 1
            last.pop(record_id, None)
            last[record_id] = end
        index = self._addenda_index()
        found: list[tuple[str, CellEntry | NoteEntry]] = []
        for record_id, n in last.items():
            entry = self._record(record_id, index)
            if entry is not None and (wanted is None or entry.kind in wanted):
                found.append((_place_after(n, after), entry))
        if after is not None and self._legacy_open():
            found += self._unnumbered_after(after, set(last), end, listed, wanted)
        return _cut(found, limit, self._place(end, began))

    def _unnumbered_after(self, after: tuple[str, str], seen: set[str], end: int, listed: dict[int, Path],
                          wanted: set[str] | None) -> list[tuple[str, CellEntry | NoteEntry]]:
        """Records changed after `after`, a (time, ref), that the numbers above did not bring: what old code wrote.
        A place from a finished read has no ref: then a little before its time too, since old code takes the time
        before it publishes (a record can appear after a read that began later than its time)."""
        if not after[1]:
            after = (_earlier(after[0], LEGACY_OVERLAP_S), "")
        announced = self._announced(after[0], end, listed)
        found = sorted(((changed, entry) for changed, entry in self._changed(wanted)
                        if (changed, entry.ref) > after and parse_ref(entry.ref)[1] not in seen
                        and announced.get(parse_ref(entry.ref)[1], "") < changed),  # (a number already says it)
                       key=lambda pair: (pair[0], pair[1].ref))
        return [(f"#{end}@{changed};{entry.ref}", entry) for changed, entry in found]

    def _announced(self, since: str, end: int, listed: dict[int, Path]) -> dict[str, str]:
        """Record id -> the latest change time the numbers up to `end` announce, looking back from `end` until
        their changes are older than `since`: a record whose last change has a number needs no second look."""
        oldest = _earlier(since, ANNOUNCED_SLACK_S)
        found: dict[str, str] = {}
        for n in range(end, max(0, end - ANNOUNCED_MAX_LOOKBACK), -1):
            path = self._listed_change(listed, n)
            if path is None:
                break
            data = read_json(path) or {}
            changed, record_id = str(data.get("changed", "")), str(data.get("id", ""))
            if changed and changed < oldest:
                break
            if changed and record_id:  # (a number written without a time says nothing)
                found[record_id] = max(found.get(record_id, ""), changed)
        return found

    def _record(self, record_id: str, index: dict[str, list[Path]]) -> CellEntry | NoteEntry | None:
        """A record as it is now, its addenda taken from `index` (see _addenda_index)."""
        if re.fullmatch(CID_PATTERN, record_id):
            base = self.raw_cell(record_id)
            if base is None:
                return None
            return _merge(base, [d for d in (read_json(p) for p in index.get(record_id, ())) if d is not None])
        return self.note(record_id) if re.fullmatch(NID_PATTERN, record_id) else None

    def _addenda_index(self) -> dict[str, list[Path]]:
        """Every addendum's path, by cell, from one listing of the folder (none of them read)."""
        grouped: dict[str, list[Path]] = {}
        if self.cells_dir.is_dir():
            for path in sorted(self.cells_dir.glob("c*.*.json")):
                cid = path.name.partition(".")[0]
                if re.fullmatch(CID_PATTERN, cid):
                    grouped.setdefault(cid, []).append(path)
        return grouped

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


def _earlier(time: str, seconds: float) -> str:
    """`time` (a stamp) some seconds before; unchanged if it is not a stamp."""
    try:
        return (parse(time) - timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    except ValueError:
        return time


def _place_after(number: int, after: tuple[str, str] | None) -> str:
    """The place after change `number`, the look at old code's records going on from `after` (a time, a ref)."""
    if after is None:
        return f"#{number}"
    return f"#{number}@{after[0]}" + (f";{after[1]}" if after[1] else "")


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
