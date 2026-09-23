"""MCP tools of the bench: a dozen general tools instead of forty brick-specific ones."""

from __future__ import annotations

from typing import Any, Callable, Literal, TypeVar

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from .audit import audited
from .bench.clients import ClientProfile, actor_for, profile_for
from .bench.files import FileView, view
from .bench.models import Actor, Checkpoint, NoteEntry
from .bench.results import CellResult, trimmed
from .bench.service import BenchService
from .bench.skills import SkillInfo, get_skill, list_skills
from .bench.views import JournalView, ProjectCard
from .datasets import DatasetEntry
from .h5ad_profile import UnsupportedFile
from .overview import Overview, cached_overview
from .projects import ProjectError, ProjectMeta
from .service import Hub, HubError
from .slurm import SlurmError
from .state import Frozen

T = TypeVar("T")
KNOWN = (HubError, SlurmError, UnsupportedFile, ProjectError, KeyError, ValueError, OSError)
NoteKindArg = Literal["registration", "decision", "finding", "error", "incident", "note", "verdict"]
Disposition = Literal["active", "waiting", "complete", "blocked"]
Scope = Literal["twin", "full", "unknown"]


class DatasetsAnswer(Frozen):
    datasets: tuple[DatasetEntry, ...] = ()
    profile: dict[str, Any] | None = None


class SkillsAnswer(Frozen):
    skills: tuple[SkillInfo, ...] = ()
    text: str = ""


class ClusterAnswer(Frozen):
    workbench: str
    overview: Overview | None = None
    problem: str = ""


def _client(ctx: Context | None) -> tuple[Actor, ClientProfile]:
    name = version = ""
    elicitation = False
    try:
        session = ctx.request_context.session if ctx is not None else None
        params = session.client_params if session is not None else None
        if params is not None:
            name, version = params.client_info.name, params.client_info.version
        caps = session.client_capabilities if session is not None else None
        elicitation = bool(caps and caps.elicitation)
    except (AttributeError, ValueError):
        pass
    return actor_for(name, version), profile_for(name, elicitation)


def register_bench_tools(mcp: MCPServer, hub: Hub, bench: BenchService) -> None:
    log_dir = hub.settings.logs_dir

    def call(tool: str, args: dict[str, Any], fn: Callable[[], T]) -> T:
        with audited(log_dir, tool, args):
            try:
                return fn()
            except KNOWN as exc:
                raise ToolError(str(exc.args[0]) if exc.args else str(exc)) from exc

    async def acall(tool: str, args: dict[str, Any], fn: Callable[[], T]) -> T:
        return await anyio.to_thread.run_sync(lambda: call(tool, args, fn))

    @mcp.tool()
    def projects() -> list[ProjectCard]:
        """Projects with their question, number of cells, where the work stands and the next action."""
        return call("projects", {}, bench.projects_list)

    @mcp.tool()
    def create_project(project: str, question: str, datasets: list[str] | None = None) -> ProjectMeta:
        """Create a project ('name' or 'parent/child', lowercase) with its scientific question in one sentence."""
        return call("create_project", {"project": project}, lambda: bench.create_project(project, question, datasets or []))

    @mcp.tool()
    async def run(project: str, code: str, why: str, expect: str, setup: bool = False,
                  data_scope: Scope = "unknown", ctx: Context = None) -> CellResult:  # type: ignore[assignment]
        """Run a cell (Python; %%bash for shell) in the project's live kernel on a compute node.
        `why`: what the cell is for; `expect`: what you expect to see. `setup`: a cell to replay after a
        kernel restart (imports, loading data). `data_scope`: twin or full data. Returns the outputs, or
        status "queued"/"running" with a ref: then call wait(ref), never run the same code again."""
        actor, profile = _client(ctx)
        args = {"project": project, "why": why[:200], "client": actor.client}
        result = await acall("run", args, lambda: bench.run(project, code, why, expect, setup=setup,
                                                             data_scope=data_scope, actor=actor,
                                                             wait_s=profile.run_wait_s))
        return trimmed(result, profile.max_output_chars)

    @mcp.tool()
    async def wait(ref: str, ctx: Context = None) -> CellResult:  # type: ignore[assignment]
        """Wait for a cell ('project#c0007') a little longer and return its result or its state."""
        actor, profile = _client(ctx)
        result = await acall("wait", {"ref": ref, "client": actor.client},
                             lambda: bench.wait(ref, profile.run_wait_s))
        return trimmed(result, profile.max_output_chars)

    @mcp.tool()
    def journal(project: str, since: str | None = None, kinds: list[str] | None = None,
                limit: int = 20) -> JournalView:
        """The project's hand-over, checkpoint and newest journal entries (cells and notes). Read it first in
        a new chat. `since`: the `newest` value of an earlier answer, to get only what is new."""
        return call("journal", {"project": project, "since": since},
                    lambda: bench.journal_view(project, since, kinds, limit))

    @mcp.tool()
    def note(project: str, kind: NoteKindArg, text: str, because: list[str] | None = None,
             reverses_if: str = "", verdict: Literal["registered", "descriptive", "invalid"] | None = None,
             audience: Literal["agent", "human", "both"] = "both",
             ctx: Context = None) -> NoteEntry:  # type: ignore[assignment]
        """Record reasoning in the journal. registration: a question and its deciding rule, before the test.
        decision: needs `because` (cell refs) and `reverses_if`. finding: needs `because`. error: your own
        mistake and what generalises. verdict: needs `verdict` and `because`."""
        actor, _ = _client(ctx)
        return call("note", {"project": project, "kind": kind},
                    lambda: bench.note(project, kind, text, because or [], reverses_if, verdict, audience, actor))

    @mcp.tool()
    def handoff(project: str, text: str, disposition: Disposition, next_action: str = "",
                waiting_jobs: list[str] | None = None, ctx: Context = None) -> Checkpoint:  # type: ignore[assignment]
        """Before you stop: where the work stands (at most 120 lines; evidence stays in the journal), and the
        next action. `waiting`: the Slurm job ids the work waits on."""
        actor, _ = _client(ctx)
        return call("handoff", {"project": project, "disposition": disposition},
                    lambda: bench.handoff(project, text, disposition, next_action, waiting_jobs or [], actor))

    @mcp.tool()
    def datasets(name: str | None = None) -> DatasetsAnswer:
        """Datasets in the shared library and the student's data/. With `name`: its metadata profile
        (counts or normalized, gene ids, species, obs columns with their levels)."""
        if name:
            return call("datasets", {"name": name},
                        lambda: DatasetsAnswer(profile=hub.inspect(name).model_dump(mode="json")))
        return call("datasets", {}, lambda: DatasetsAnswer(datasets=tuple(hub.datasets())))

    @mcp.tool()
    def files(project: str | None = None, path: str = ".", max_chars: int = 20_000) -> FileView:
        """List a folder or read a text file inside a project or the libraries (read-only; cells write).
        An .h5ad answers with its metadata profile."""
        return call("files", {"project": project, "path": path},
                    lambda: view(hub.settings, project, path, max(100, min(max_chars, 100_000))))

    @mcp.tool()
    def skills(name: str | None = None) -> SkillsAnswer:
        """Playbooks for kinds of work (resume, rigor, mbzuai_slurm...). Without `name`: the list."""
        if name:
            return call("skills", {"name": name}, lambda: SkillsAnswer(text=get_skill(name)))
        return call("skills", {}, lambda: SkillsAnswer(skills=tuple(list_skills())))

    @mcp.tool()
    def cluster() -> ClusterAnswer:
        """The workbench, your job slots, quotas, limits, your jobs and the cluster's load."""
        def answer() -> ClusterAnswer:
            try:
                overview, problem = cached_overview(hub.settings), ""
            except (OSError, ValueError) as exc:
                overview, problem = None, f"overview unavailable: {exc}"
            return ClusterAnswer(workbench=bench.status(), overview=overview, problem=problem)
        return call("cluster", {}, answer)

    @mcp.tool()
    def stop(target: str) -> str:
        """Interrupt a running cell ('project#c0007'), or 'workbench' to stop the workbench now and free
        its job slot (its variables are lost; files stay)."""
        return call("stop", {"target": target}, lambda: bench.stop(target))
