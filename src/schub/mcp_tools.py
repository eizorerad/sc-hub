"""MCP tools for recipes, FASTQ datasets, Seurat imports and interactive sessions."""

from __future__ import annotations

from typing import Any, Callable, Literal, TypeVar

from mcp.server import MCPServer

from .datasets import DatasetEntry
from .recipes import Recipe
from .seurat import ImportJob
from .service import Hub
from .sessions import SessionInfo

T = TypeVar("T")
Call = Callable[[str, dict[str, Any], Callable[[], T]], T]


def register_tools(mcp: MCPServer, hub: Hub, call: Call) -> None:
    @mcp.tool()
    def list_recipes() -> list[Recipe]:
        """Pipeline templates that follow the course defaults (scverse + scvi-tools):
        pick the one that matches the data, fill its placeholders from inspect_dataset
        (confirm with the user), then save_branch with the filled steps."""
        return call("list_recipes", {}, hub.recipes)

    @mcp.tool()
    def recipe_steps(name: str, values: dict[str, str]) -> list[dict[str, Any]]:
        """A recipe's steps with its "<placeholders>" filled, ready for save_branch(steps=...)."""
        return call("recipe_steps", {"name": name}, lambda: hub.recipe_steps(name, values))

    @mcp.tool()
    def register_fastq(
        folder: str, technology: str, organism: Literal["human", "mouse"], title: str = "",
        expected_cells: int | None = None,
    ) -> DatasetEntry:
        """Describe a folder of 10x FASTQ files under data/ (names like
        <sample>_S1_L001_R1_001.fastq.gz) so it can be planned with kb_count or
        cellranger_count. technology: 10xv2, 10xv3 (ask the user; it is on the kit)."""
        args = {"folder": folder, "technology": technology, "organism": organism}
        return call("register_fastq", args, lambda: hub.register_fastq(folder, technology, organism, title, expected_cells))

    @mcp.tool()
    def import_seurat(rds: str, name: str) -> ImportJob:
        """Queue a job converting a Seurat object (.rds under data/) into data/<name>/data.h5ad
        (counts, metadata, embeddings). Plan on it by name once the job has finished."""
        return call("import_seurat", {"rds": rds, "name": name}, lambda: hub.import_seurat(rds, name))

    @mcp.tool()
    def start_session(
        kind: Literal["jupyter", "cellxgene"], hours: int = 4, gpu: bool = False, target: str = ""
    ) -> SessionInfo:
        """Start JupyterLab (optionally with 1 GPU, for scvi-tools model work) or cellxgene
        (target = an .h5ad, e.g. results/cellxgene.h5ad of export_cellxgene) on a compute
        node. It holds one of the user's 2 running-job slots until stopped or expired, so
        use few hours and stop it when done. The user opens it with ./schub-lab <kind>."""
        args = {"kind": kind, "hours": hours, "gpu": gpu, "target": target}
        return call("start_session", args, lambda: hub.start_session(kind, hours, gpu, target))

    @mcp.tool()
    def list_sessions() -> list[SessionInfo]:
        """The user's interactive sessions (running, queued, or ended in the last day)."""
        return call("list_sessions", {}, hub.list_sessions)

    @mcp.tool()
    def stop_session(session_id: str) -> SessionInfo:
        """Stop an interactive session (frees its job slot)."""
        return call("stop_session", {"session_id": session_id}, lambda: hub.stop_session(session_id))
