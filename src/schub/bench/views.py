"""Compact views of projects and journals for tool answers.

Notes written for the student (audience "human") are shown to the agent only as a
placeholder: in VCC2026 an agent executed a note meant for the owner and cancelled
the owner's terminal twice (incident A2). Text in the journal is data, not orders.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..config import Settings
from ..projects import ProjectError, ProjectStore
from ..state import Frozen
from .checkpoint import CheckpointStore
from .journal import Journal
from .models import CellEntry, Checkpoint, NoteEntry

MAX_FILES_SHOWN = 10
HUMAN_ONLY = "(a note for the student; not shown to the assistant)"


class ProjectCard(Frozen):
    project: str
    question: str = ""
    cells: int = 0
    last_entry: str = ""
    disposition: str = "active"
    next_action: str = ""
    handoff_first_line: str = ""


class JournalView(Frozen):
    project: str
    handoff: str
    checkpoint: Checkpoint
    entries: tuple[dict[str, Any], ...]
    newest: str = ""  # pass as `since` next time to get only what is new


def _trim(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[: budget * 2 // 3] + "\n[...]\n" + text[-budget // 3:]


def compact(entry: CellEntry | NoteEntry, max_chars: int = 1500, for_agent: bool = True) -> dict[str, Any]:
    if isinstance(entry, NoteEntry):
        hidden = for_agent and entry.audience == "human"
        return {
            "ref": entry.ref, "kind": entry.kind, "created": entry.created, "audience": entry.audience,
            "text": HUMAN_ONLY if hidden else _trim(entry.text, max_chars), "because": list(entry.because),
            "reverses_if": entry.reverses_if, "verdict": entry.verdict, "by": entry.actor.client or entry.actor.kind,
            "unresolved_numbers": list(entry.unresolved_numbers),
        }
    per_output = max(200, max_chars // max(1, len(entry.outputs)))
    return {
        "ref": entry.ref, "kind": "cell", "created": entry.created, "status": entry.status,
        "why": entry.why, "expect": entry.expect, "data_scope": entry.data_scope, "setup": entry.setup,
        "duration_s": entry.duration_s, "kernel_epoch": entry.kernel_epoch, "message": entry.message,
        "outputs": [{"kind": o.kind, "text": _trim(o.text, per_output), "image": o.image, "ename": o.ename}
                    for o in entry.outputs],
        "files": [f"{f.change} {f.path}" for f in entry.files[:MAX_FILES_SHOWN]],
        "more_files": max(0, len(entry.files) - MAX_FILES_SHOWN),
        "jobs": [f"{j.job_id} {j.state}" for j in entry.jobs],
        "checks": [f"{c.name}: {c.status}" + (f" ({c.message})" if c.message else "") for c in entry.check_results],
        "by": entry.actor.client or entry.actor.kind,
    }


def journal_view(settings: Settings, project: str, since: str | None, kinds: Iterable[str] | None,
                 limit: int, max_chars: int) -> JournalView:
    project_dir = ProjectStore(settings).require(project)
    journal = Journal(project_dir, project)
    store = CheckpointStore(project_dir)
    entries = journal.entries(since=since, kinds=kinds, limit=limit)
    budget = max(300, max_chars // max(1, len(entries)))
    return JournalView(
        project=project, handoff=store.read_handoff(), checkpoint=store.read(),
        entries=tuple(compact(e, budget) for e in entries), newest=entries[-1].created if entries else (since or ""),
    )


def project_cards(settings: Settings) -> list[ProjectCard]:
    store = ProjectStore(settings)
    cards = []
    for name in store.names():
        try:
            meta = store.meta(name)
            project_dir = store.require(name)
        except ProjectError:
            continue
        journal = Journal(project_dir, name)
        latest = journal.latest_cells(1)
        checkpoint = CheckpointStore(project_dir).read()
        handoff = CheckpointStore(project_dir).read_handoff().strip().splitlines()
        ids = journal.folder / "ids"
        cells = sum(1 for p in ids.iterdir() if p.name.startswith("c")) if ids.is_dir() else 0
        cards.append(ProjectCard(
            project=name, question=meta.question, cells=cells, last_entry=latest[-1].created if latest else "",
            disposition=checkpoint.disposition, next_action=checkpoint.next_action,
            handoff_first_line=handoff[0][:200] if handoff else "",
        ))
    return cards
