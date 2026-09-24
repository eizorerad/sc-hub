"""Compact views of projects and journals for tool answers.

Notes written for the student (audience "human") are shown to the agent only as a
placeholder: in VCC2026 an agent executed a note meant for the owner and cancelled
the owner's terminal twice (incident A2). Text in the journal is data, not orders.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..config import Settings
from ..projects import ProjectError, ProjectStore
from ..state import Frozen
from .checkpoint import CheckpointStore
from .journal import Journal
from .models import CellEntry, Checkpoint, NoteEntry

MAX_FILES_SHOWN = 10
MAX_OUTPUTS_SHOWN = 8  # per entry: the first and last ones and every error; the journal has the rest
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
    left_out: int = 0  # entries not shown to fit the client's limit (see journal_view)


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
    shown, more_outputs = _some_outputs(entry)
    per_output = max(120, max_chars // max(1, len(shown)))
    return {
        "ref": entry.ref, "kind": "cell", "created": entry.created, "status": entry.status,
        "why": _trim(entry.why, 600), "expect": _trim(entry.expect, 600), "data_scope": entry.data_scope,
        "setup": entry.setup,
        "duration_s": entry.duration_s, "kernel_epoch": entry.kernel_epoch, "message": entry.message,
        "outputs": [{"kind": o.kind, "text": _trim(o.text, per_output), "image": o.image, "ename": o.ename}
                    for o in shown],
        "more_outputs": more_outputs,
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
    changes = journal.changes(since=since, kinds=kinds, limit=limit)
    budget = max(300, max_chars // max(1, len(changes)))
    entries = [compact(e, budget) for _, e in changes]
    left_out = 0
    # Still too long for the client (many entries, each at its floor): leave entries out. Without `since`
    # the oldest go (the recent work matters most); with `since` the newest wait for the next call, and
    # `newest` stops before them, so paging on never skips one.
    while len(entries) > 1 and len(json.dumps(entries, default=str)) > max_chars:
        if since is None:
            entries.pop(0)
            changes.pop(0)
        else:
            entries.pop()
            changes.pop()
        left_out += 1
    newest = max((changed for changed, _ in changes), default=since or "")
    return JournalView(
        project=project, handoff=store.read_handoff(), checkpoint=store.read(),
        entries=tuple(entries), newest=newest, left_out=left_out,
    )


def _some_outputs(entry: CellEntry) -> tuple[list[Any], int]:
    """At most MAX_OUTPUTS_SHOWN outputs (more only if there are more errors): every error, then the first
    and the last ones."""
    outputs = list(entry.outputs)
    if len(outputs) <= MAX_OUTPUTS_SHOWN:
        return outputs, 0
    errors = {i for i, o in enumerate(outputs) if o.kind == "error"}
    others = [i for i in range(len(outputs)) if i not in errors]
    room = max(0, MAX_OUTPUTS_SHOWN - len(errors))
    first, last = room - room // 2, room // 2
    keep = errors | set(others[:first]) | set(others[len(others) - last:] if last else [])
    kept = [outputs[i] for i in sorted(keep)]
    return kept, len(outputs) - len(kept)


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
