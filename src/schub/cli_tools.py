"""CLI commands for recipes, FASTQ datasets, Seurat imports and interactive sessions."""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from .service import Hub
from .sessions import KINDS, MAX_HOURS


def add_tool_parsers(sub: Any) -> None:
    sub.add_parser("recipes", help="course-aligned pipeline templates")
    fill = sub.add_parser("recipe", help="steps of a recipe with its placeholders filled")
    fill.add_argument("name")
    fill.add_argument("values", nargs="?", default="{}", help='JSON, e.g. {"batch_key": "sample"}')
    fastq = sub.add_parser("register-fastq", help="describe a folder of 10x FASTQ files in data/ (writes fastq.yaml)")
    fastq.add_argument("folder", help="folder inside data/")
    fastq.add_argument("--technology", required=True, help="10xv2, 10xv3, ...")
    fastq.add_argument("--organism", required=True, choices=["human", "mouse"])
    fastq.add_argument("--title", default="")
    fastq.add_argument("--expected-cells", type=int)
    seurat = sub.add_parser("import-seurat", help="queue a job that converts a Seurat .rds into data/<name>")
    seurat.add_argument("rds")
    seurat.add_argument("name")
    start = sub.add_parser("session-start", help="start JupyterLab or cellxgene on a compute node")
    start.add_argument("kind", choices=KINDS)
    start.add_argument("--hours", type=int, default=4, choices=range(1, MAX_HOURS + 1), metavar=f"1-{MAX_HOURS}")
    start.add_argument("--gpu", action="store_true")
    start.add_argument("--target", default="", help="notebook/folder (jupyter) or .h5ad (cellxgene)")
    sub.add_parser("sessions", help="your interactive sessions")
    sub.add_parser("session-stop", help="stop a session").add_argument("session_id")
    sub.add_parser("session-info", help="'<node> <port> <path>' of the running session (for schub-lab)").add_argument(
        "kind", choices=KINDS
    )


def tool_handlers(hub: Hub, args: argparse.Namespace) -> dict[str, Callable[[], Any]]:
    return {
        "recipes": lambda: hub.recipes(),
        "recipe": lambda: hub.recipe_steps(args.name, json.loads(args.values)),
        "register-fastq": lambda: hub.register_fastq(
            args.folder, args.technology, args.organism, args.title, args.expected_cells
        ),
        "import-seurat": lambda: hub.import_seurat(args.rds, args.name),
        "session-start": lambda: hub.start_session(args.kind, args.hours, args.gpu, args.target),
        "sessions": lambda: hub.list_sessions(),
        "session-stop": lambda: hub.stop_session(args.session_id),
        "session-info": lambda: hub.session_line(args.kind),
    }
