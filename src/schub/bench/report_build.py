"""A report spec assembled into a notebook from the journal (nbformat 4.5).

Code, outputs, figures and notes come verbatim from the journal; only the prose is
the author's. The checks are lenient on purpose (the bench serves very different
requests): a report is refused only when a block points at nothing. Everything else
becomes a warning the author sees and the reader finds at the end of the report.
Findings, verdicts, decisions and mistakes the text leaves out are listed in an
appendix, so a negative result cannot quietly disappear.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from .journal import Journal, JournalError, parse_ref
from .models import Actor, CellEntry, NoteEntry
from .numbers import unresolved
from .render_nb import NOTE_TITLES, _id, _image, _lines, _markdown, _note, _outputs
from .report_spec import Block, ReportSpec

APPENDIX_KINDS = ("finding", "verdict", "decision", "error", "registration")
UNFINISHED = ("queued", "running")
FAILED = ("error", "lost", "interrupted", "retired")
FIGURE = re.compile(r"^(?P<cell>c\d{4,9})(?::(?P<index>\d{1,3}))?$")
MAX_LISTED = 12


class ReportError(ValueError):
    pass


@dataclass(frozen=True)
class Built:
    notebook: dict[str, Any]
    warnings: tuple[str, ...]
    covers: str  # the newest cell in the journal
    cited: tuple[str, ...]  # journal ids the report shows


def _local(ref: str, project: str, prefix: str) -> str:
    """'c0012' or 'project#c0012' -> 'c0012'; anything else is refused."""
    text = ref.strip()
    if "#" in text:
        try:
            owner, text = parse_ref(text)
        except JournalError as exc:
            raise ReportError(str(exc)) from exc
        if owner != project:
            raise ReportError(f"{ref} belongs to another project")
    if not re.fullmatch(rf"{prefix}\d{{4,9}}(:\d{{1,3}})?", text):
        raise ReportError(f"'{ref}' is not a {'cell' if prefix == 'c' else 'note'} id like {prefix}0007")
    return text


class Assembler:
    def __init__(self, journal: Journal, project: str, question: str, spec: ReportSpec) -> None:
        self.journal, self.project, self.question, self.spec = journal, project, question, spec
        entries = journal.entries()
        self.cells = {e.cid: e for e in entries if isinstance(e, CellEntry)}
        self.notes = {e.nid: e for e in entries if isinstance(e, NoteEntry)}
        self.order = [e.cid for e in entries if isinstance(e, CellEntry)]
        self.errors: list[str] = []  # blocks that point at nothing: the report is refused
        self.notices: list[str] = []  # found while assembling; reported with the warnings
        self.cited: dict[str, None] = {}

    # ---- blocks ---------------------------------------------------------------------------

    def block(self, number: int, block: Block) -> list[dict[str, Any]]:
        try:
            if block.kind == "text":
                return [_markdown(_id(self.project, "text", str(number)), block.text)]
            if block.kind == "note":
                return self._note(number, _local(block.note, self.project, "n"))
            if block.kind == "figure":
                return self._figure(number, block)
            return self._cell(number, _local(block.cell, self.project, "c"), block)
        except ReportError as exc:
            self.errors.append(f"block {number}: {exc}")
            return []

    def _entry(self, cid: str) -> CellEntry:
        entry = self.cells.get(cid)
        if entry is None:
            raise ReportError(f"{cid} is not a cell of {self.project}")
        self.cited[cid] = None
        return entry

    def _caption(self, entry: CellEntry, caption: str) -> str:
        facts = [f"cell {entry.cid}", entry.status, entry.created[:16].replace("T", " ")]
        facts += [f"job {j.job_id} {j.state.lower()}" for j in entry.jobs]
        facts += [f"check {c.name}: {c.status}" for c in entry.check_results]
        if entry.data_scope == "twin":
            facts.append("on a twin (small copy of the data)")
        return f"*{caption or entry.why}*  \n{' · '.join(facts)}"

    def _cell(self, number: int, cid: str, block: Block) -> list[dict[str, Any]]:
        entry = self._entry(cid)
        outputs = _outputs(entry, self.journal.folder) if block.show != "code" else []
        metadata: dict[str, Any] = {"schub_ref": entry.ref}
        if block.show == "outputs":
            metadata["jupyter"] = {"source_hidden": True}
        code = {"cell_type": "code", "id": _id(self.project, "cell", cid, str(number)), "metadata": metadata,
                "execution_count": None, "source": _lines(entry.code), "outputs": outputs}
        return [_markdown(_id(self.project, "cap", cid, str(number)), self._caption(entry, block.caption)), code]

    def _figure(self, number: int, block: Block) -> list[dict[str, Any]]:
        match = FIGURE.fullmatch(_local(block.figure, self.project, "c"))
        if match is None:
            raise ReportError(f"'{block.figure}' is not a figure like c0015 or c0015:2")
        entry = self._entry(match["cell"])
        images = [o.image for o in entry.outputs if o.image]
        index = int(match["index"] or 1)
        if not images:
            raise ReportError(f"{entry.cid} has no figure")
        if index > len(images):
            raise ReportError(f"{entry.cid} has {len(images)} figure(s), not {index}")
        data = _image(self.journal.folder / images[index - 1])
        if not data:  # the figure exists in the journal; the report just cannot carry it
            self.notices.append(f"figure {index} of {entry.cid} is missing or larger than 2 MB: the report names it")
            return [_markdown(_id(self.project, "fig", entry.cid, str(index), str(number)),
                              f"*Figure {index} of {entry.cid} ({entry.why}) is in the journal: "
                              f"journal/{images[index - 1]}*")]
        name = f"{entry.cid}-{index}.png"
        text = f"![{block.caption or entry.why}](attachment:{name})\n\n" \
               f"*{block.caption or entry.why}* ({entry.cid})"
        cell = _markdown(_id(self.project, "fig", entry.cid, str(index), str(number)), text)
        return [{**cell, "attachments": {name: {"image/png": data}}}]

    def _note(self, number: int, nid: str) -> list[dict[str, Any]]:
        note = self.notes.get(nid)
        if note is None:
            raise ReportError(f"{nid} is not a note of {self.project}")
        self.cited[nid] = None
        if note.audience == "human":
            raise ReportError(f"{nid} is a note for the student only; quote what matters in a text block")
        return [{**_note(note), "id": _id(self.project, "note", nid, str(number))}]

    # ---- warnings -------------------------------------------------------------------------

    def warnings(self) -> list[str]:
        cells = [self.cells[c] for c in self.cited if c in self.cells]
        found = self.notices + self._numbers(cells) + self._states(cells) + self._overwritten(cells)
        twins = [c.cid for c in cells if c.data_scope == "twin"]
        if twins:
            found.append(f"{_few(twins)} ran on twins (small copies of the data): say so where their numbers are used")
        if not self.spec.summary.strip():
            found.append("no summary: readers look for the question and the answer first")
        unknown = [r for r in self.spec.left_out if not self._known(r)]
        if unknown:
            found.append(f"left_out names {_few(unknown)}, which are not in this journal (ignored)")
        missing = self.not_shown()
        if missing:
            found.append(f"{len(missing)} finding(s), verdict(s), decision(s) or mistake(s) are not in the text; the "
                         "appendix 'Also in the journal' lists them" + (" with your reasons" if self.spec.left_out else ""))
        return found

    def _numbers(self, cells: Sequence[CellEntry]) -> list[str]:
        prose = "\n".join([self.spec.summary] + [b.text for b in self.spec.blocks] + [b.caption for b in self.spec.blocks])
        evidence = [o.text for c in cells for o in c.outputs] + [m.message for c in cells for m in c.check_results]
        evidence += [self.notes[n].text for n in self.cited if n in self.notes]
        missing = unresolved(prose, evidence)
        if not missing:
            return []
        return [f"numbers in the text not found in the cells and notes the report shows: {_few(missing)} (show the "
                "cell they come from, or say where they come from)"]

    def _states(self, cells: Sequence[CellEntry]) -> list[str]:
        found = []
        for entry in cells:
            if entry.status in UNFINISHED:
                found.append(f"{entry.cid} is still {entry.status}: its outputs may be incomplete")
            elif entry.status in FAILED:
                found.append(f"{entry.cid} ended as '{entry.status}' (shown as it is)")
            latest: dict[str, str] = {}
            for check in entry.check_results:
                latest[check.name] = check.status
            failing = [name for name, status in latest.items() if status in ("fail", "error")]
            if failing:
                found.append(f"{entry.cid}: check {', '.join(failing)} failed")
        return found

    def _overwritten(self, cells: Sequence[CellEntry]) -> list[str]:
        found = []
        position = {cid: i for i, cid in enumerate(self.order)}
        for entry in cells:
            written = {f.path for f in entry.files if f.change in ("created", "modified")}
            for later in self.order[position[entry.cid] + 1:]:
                changed = [f.path for f in self.cells[later].files if f.path in written]
                if changed and later not in self.cited:
                    found.append(f"{_few(changed)} from {entry.cid} changed later in {later}")
                    written -= set(changed)
        return found

    def _known(self, ref: str) -> bool:
        owner, _, local = ref.strip().rpartition("#")
        return (not owner or owner == self.project) and (local in self.cells or local in self.notes)

    def not_shown(self) -> list[NoteEntry]:
        return [n for n in self.notes.values() if n.kind in APPENDIX_KINDS and n.nid not in self.cited
                and n.audience != "human"]

    # ---- the frame ---------------------------------------------------------------------------

    def head(self, covers: str, when: str) -> list[dict[str, Any]]:
        spec = self.spec
        top = f"# {spec.title}\n\n*{self.project}* — {self.question}".rstrip(" —")
        if spec.summary.strip():
            top += f"\n\n{spec.summary}"
        upto = covers or "its start"
        about = (f"*This report was assembled by sc-hub from the journal of **{self.project}** up to {upto} "
                 f"({when[:10]}). Its code cells show what ran on the cluster, with the outputs recorded then; "
                 "they are not re-run here. Every step, dead ends included, is in the project's protocol notebook "
                 "(the dashboard's Journal ⋯ Download as a notebook).*")
        return [_markdown(_id(self.project, "title"), top), _markdown(_id(self.project, "about"), about)]

    def appendix(self, warnings: Sequence[str], actor: Actor, when: str, covers: str) -> list[dict[str, Any]]:
        cells: list[dict[str, Any]] = []
        missing = self.not_shown()
        if missing:
            reasons = {self._reason_key(r): why for r, why in self.spec.left_out.items()}
            lines = [f"- **{NOTE_TITLES.get(n.kind, n.kind)}** {n.nid}: {_short(n.text, 300)}"
                     + (f" — *left out:* {reasons[n.nid]}" if n.nid in reasons else "") for n in missing]
            cells.append(_markdown(_id(self.project, "also"), "## Also in the journal\n\n" + "\n".join(lines)))
        sources = self._sources()
        if sources:
            cells.append(_markdown(_id(self.project, "sources"), "## Where the data and code came from\n\n" + sources))
        made = actor.engine or actor.client or actor.kind
        notes = [f"- built {when[:16].replace('T', ' ')} UTC by {made}; covers the journal up to {covers}"]
        notes += [f"- {w}" for w in warnings]
        cells.append(_markdown(_id(self.project, "checks"), "## Notes on this report\n\n" + "\n".join(notes)))
        return cells

    def _reason_key(self, ref: str) -> str:
        return ref.strip().rpartition("#")[2]

    def _sources(self) -> str:
        downloads: dict[str, str] = {}
        bricks: dict[str, None] = {}
        workers: dict[str, None] = {}
        for entry in self.cells.values():
            for d in entry.downloads:
                if d.status == "ok":
                    downloads.setdefault(d.url, f"commit {d.commit[:12]}" if d.commit else
                                         f"sha256 {d.sha256[:16]}…, {d.size:,} bytes")
            bricks.update(dict.fromkeys(entry.bricks))
            workers[entry.actor.engine or entry.actor.client or entry.actor.kind] = None
        jobs = [f"{j.job_id} ({c.cid}, {j.state.lower()})" for c in (self.cells[i] for i in self.cited
                                                                     if i in self.cells) for j in c.jobs]
        lines = [f"- `{url}` — {what}" for url, what in downloads.items()]
        if bricks:
            lines.append(f"- sc-hub bricks: {', '.join(bricks)}")
        if jobs:
            lines.append(f"- Slurm jobs of the cells shown: {', '.join(jobs)}")
        if workers:
            lines.append(f"- worked on by: {', '.join(workers)}")
        return "\n".join(lines)


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _few(items: Sequence[str]) -> str:
    items = list(dict.fromkeys(items))
    shown = ", ".join(items[:MAX_LISTED])
    return shown + (f" and {len(items) - MAX_LISTED} more" if len(items) > MAX_LISTED else "")


def build(journal: Journal, project: str, question: str, spec: ReportSpec, actor: Actor, when: str) -> Built:
    """The notebook of `spec`, or ReportError listing every block that points at nothing."""
    assembler = Assembler(journal, project, question, spec)
    body = [cell for number, block in enumerate(spec.blocks, start=1) for cell in assembler.block(number, block)]
    if assembler.errors:
        raise ReportError("; ".join(assembler.errors))
    covers = assembler.order[-1] if assembler.order else ""
    warnings = tuple(assembler.warnings())
    cells = assembler.head(covers, when) + body + assembler.appendix(warnings, actor, when, covers)
    notebook = {
        "nbformat": 4, "nbformat_minor": 5, "cells": cells,
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                     "language_info": {"name": "python"},
                     "schub": {"project": project, "source": "report", "covers": covers, "built": when}},
    }
    return Built(notebook=notebook, warnings=warnings, covers=covers, cited=tuple(assembler.cited))
