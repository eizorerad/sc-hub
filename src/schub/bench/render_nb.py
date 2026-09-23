"""The journal as a Jupyter notebook (nbformat 4.5): each cell with its why and
expect as markdown, its code, and its recorded outputs; notes as markdown. A
%%slurm cell keeps its header, so the notebook shows what went to Slurm."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from .models import CellEntry, NoteEntry

MAX_IMAGE_BYTES = 2_000_000
NOTE_TITLES = {"registration": "Registered before the result", "decision": "Decision", "finding": "Finding",
               "error": "Mistake", "incident": "Incident", "verdict": "Verdict", "handoff": "Hand-over",
               "note": "Note"}


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _markdown(cell_id: str, text: str) -> dict[str, Any]:
    return {"cell_type": "markdown", "id": cell_id, "metadata": {}, "source": _lines(text)}


def _outputs(entry: CellEntry, journal_dir: Path) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for item in entry.outputs:
        if item.kind == "stream":
            outputs.append({"output_type": "stream", "name": item.name or "stdout", "text": _lines(item.text)})
        elif item.kind == "error":
            outputs.append({"output_type": "error", "ename": item.ename, "evalue": item.evalue,
                            "traceback": _lines(item.text)})
        elif item.image:
            data = _image(journal_dir / item.image)
            if data:
                outputs.append({"output_type": "display_data", "metadata": {}, "data": {"image/png": data}})
        else:
            outputs.append({"output_type": "display_data", "metadata": {}, "data": {"text/plain": _lines(item.text)}})
    return outputs


def _image(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            return ""
        return base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return ""


def _cell(entry: CellEntry, journal_dir: Path) -> list[dict[str, Any]]:
    head = f"**{entry.ref}** · {entry.status} · {entry.created[:16].replace('T', ' ')}\n\n*Why:* {entry.why}\n\n" \
           f"*Expected:* {entry.expect}"
    extras = [f"- job {j.job_id}: {j.state}" for j in entry.jobs] + \
             [f"- check {c.name}: {c.status} {c.message}" for c in entry.check_results]
    if extras:
        head += "\n\n" + "\n".join(extras)
    code = {"cell_type": "code", "id": _id(entry.ref, "code"), "metadata": {"schub_ref": entry.ref},
            "execution_count": None, "source": _lines(entry.code), "outputs": _outputs(entry, journal_dir)}
    return [_markdown(_id(entry.ref, "head"), head), code]


def _note(entry: NoteEntry) -> dict[str, Any]:
    text = f"**{NOTE_TITLES.get(entry.kind, entry.kind)}** ({entry.ref})\n\n{entry.text}"
    if entry.because:
        text += f"\n\n*Because:* {', '.join(entry.because)}"
    if entry.reverses_if:
        text += f"\n\n*Reversed if:* {entry.reverses_if}"
    return _markdown(_id(entry.ref), text)


def render(project: str, question: str, entries: list[CellEntry | NoteEntry], journal_dir: Path) -> dict[str, Any]:
    cells = [_markdown(_id(project, "title"), f"# {project}\n\n{question}".strip())]
    for entry in entries:
        cells += _cell(entry, journal_dir) if isinstance(entry, CellEntry) else [_note(entry)]
    return {
        "nbformat": 4, "nbformat_minor": 5, "cells": cells,
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                     "language_info": {"name": "python"}, "schub": {"project": project, "source": "journal"}},
    }
