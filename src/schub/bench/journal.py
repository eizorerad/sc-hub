"""The project journal: one file per record, append-only.

    projects/<p>/journal/
      ids/c0007                 id markers (allocation by exclusive create)
      cells/c0007.json          a cell; written only by the runner, frozen once final
      cells/c0007.job-812.json  addenda (a job ended, a check ran); created once, never changed
      cells/c0007/              artifacts (figures)
      notes/n0003.json          decisions, findings, errors, registrations...

Ids are handed out by the server when a record is appended (VCC2026 once gave two
findings the same number, F48/F49). A decision must name the cells it rests on and
the condition that would reverse it; a finding must name its cells.
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
    MAX_TEXT_CHARS, NOTE_KINDS, Actor, CellEntry, CheckResult, Download, FileChange, JobRef, NoteEntry,
)

REF = re.compile(r"^(?P<project>[a-z0-9][a-z0-9_/-]*)#(?P<id>[cn]\d{4,})$")
SUFFIX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
ADDENDUM_KINDS = ("job", "check", "download", "files")
NEEDS_BECAUSE = ("decision", "finding", "verdict")


class JournalError(ValueError):
    pass


def parse_ref(ref: str) -> tuple[str, str]:
    match = REF.fullmatch(ref.strip())
    if match is None:
        raise JournalError(f"'{ref}' is not a journal reference like 'project#c0007'")
    return match["project"], match["id"]


def _number(record_id: str) -> int:
    return int(record_id[1:])


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

    def ref(self, record_id: str) -> str:
        return f"{self.project}#{record_id}"

    def artifacts_dir(self, cid: str) -> Path:
        return self.cells_dir / cid

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
                os.close(os.open(ids / record_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
                return record_id
            except FileExistsError:
                n += 1

    # ---- cells ----------------------------------------------------------------

    def write_cell(self, entry: CellEntry) -> None:
        """Only the runner calls this. A final entry is never rewritten."""
        path = self.cells_dir / f"{entry.cid}.json"
        current = read_json(path)
        if current is not None and current.get("status") in ("ok", "error", "interrupted", "lost", "retired"):
            raise JournalError(f"{entry.ref} is final ({current['status']}); add an addendum instead")
        write_json_atomic(path, entry.model_dump(mode="json"))

    def raw_cell(self, cid: str) -> CellEntry | None:
        data = read_json(self.cells_dir / f"{cid}.json")
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
        if not SUFFIX.fullmatch(suffix):
            raise JournalError(f"bad addendum name {suffix!r}")
        if payload.get("kind") not in ADDENDUM_KINDS:
            raise JournalError(f"addendum kind must be one of {ADDENDUM_KINDS}")
        stamped = {**payload, "added": self.now()}
        return create_json_exclusive(self.cells_dir / f"{cid}.{suffix}.json", stamped)

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
    ) -> NoteEntry:
        text, reverses_if = text.strip(), reverses_if.strip()
        self._check_note(kind, text, because, reverses_if, verdict)
        nid = self.allocate("n")
        note = NoteEntry(
            kind=kind, ref=self.ref(nid), project=self.project, nid=nid, text=text,
            because=tuple(because), reverses_if=reverses_if, verdict=verdict, audience=audience,
            actor=actor or Actor(), created=self.now(),
        )
        if not create_json_exclusive(self.notes_dir / f"{nid}.json", note.model_dump(mode="json")):
            raise JournalError(f"note {nid} already exists")
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

    def exists(self, record_id: str) -> bool:
        folder = self.cells_dir if record_id.startswith("c") else self.notes_dir
        return (folder / f"{record_id}.json").is_file()

    def note(self, nid: str) -> NoteEntry | None:
        data = read_json(self.notes_dir / f"{nid}.json")
        try:
            return NoteEntry.model_validate(data) if data is not None else None
        except ValidationError:
            return None

    # ---- reading ----------------------------------------------------------------

    def entries(
        self, since: str | None = None, kinds: Iterable[str] | None = None, limit: int | None = None,
    ) -> list[CellEntry | NoteEntry]:
        wanted = set(kinds) if kinds is not None else None
        found: list[CellEntry | NoteEntry] = []
        for record_id in self._ids():
            entry = self.cell(record_id) if record_id.startswith("c") else self.note(record_id)
            if entry is None or (wanted is not None and entry.kind not in wanted):
                continue
            if since is not None and entry.created <= since:
                continue
            found.append(entry)
        found.sort(key=lambda e: (e.created, e.ref))
        return found[-limit:] if limit else found

    def _ids(self) -> list[str]:
        ids: list[str] = []
        for folder in (self.cells_dir, self.notes_dir):
            if folder.is_dir():
                ids += [p.stem for p in folder.glob("*.json") if "." not in p.stem and p.stem[1:].isdigit()]
        return sorted(ids, key=lambda i: (i[0], _number(i)))


def _merge(base: CellEntry, addenda: list[dict[str, Any]]) -> CellEntry:
    jobs = {j.job_id: j for j in base.jobs}
    checks = {c.name: c for c in base.check_results}
    downloads = list(base.downloads)
    files = list(base.files)
    for addendum in addenda:
        try:
            kind = addendum.get("kind")
            if kind == "job":
                job = JobRef.model_validate(addendum["job"])
                jobs[job.job_id] = jobs[job.job_id].model_copy(update=addendum["job"]) if job.job_id in jobs else job
            elif kind == "check":
                check = CheckResult.model_validate(addendum["check"])
                checks[check.name] = check
            elif kind == "download":
                downloads.append(Download.model_validate(addendum["download"]))
            elif kind == "files":
                files += [FileChange.model_validate(f) for f in addendum["files"]]
        except (KeyError, TypeError, ValidationError):
            continue  # a malformed addendum is skipped, never fatal to reading the journal
    return base.model_copy(update={
        "jobs": tuple(jobs.values()), "check_results": tuple(checks.values()),
        "downloads": tuple(downloads), "files": tuple(files),
    })
