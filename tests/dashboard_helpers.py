"""Reading the graph files (br/<view>.js) the Pipelines view loads on demand."""

from __future__ import annotations

import json
from pathlib import Path


def br_payload(text: str) -> dict:
    """`(window.SCHUB_BR = ...)["v-x"] = {...};` -> the {graph, templates} object."""
    return json.loads(text[text.index("] = ") + 4:].rstrip().rstrip(";"))


def graph_of(files: dict[str, str], view: str) -> str:
    return br_payload(files[f"br/{view}.js"])["graph"]


def templates_of(files: dict[str, str], view: str) -> str:
    return br_payload(files[f"br/{view}.js"])["templates"]


def variant(graph: str, history: bool) -> str:
    """The graph as things are now (history=False) or with older versions."""
    part = graph.split(f'data-history="{int(history)}"', 1)
    return part[1].split("data-history=", 1)[0] if len(part) == 2 else ""


def view_files(view_dir: Path) -> dict[str, str]:
    return {f"br/{p.name}": p.read_text() for p in sorted((view_dir / "br").glob("*.js"))}
