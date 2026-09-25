"""MCP tools of the bench: fifteen general tools instead of forty brick-specific ones."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

import base64
import json

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from .audit import audited
from .bench.clients import ClientProfile, actor_for, profile_for
from .bench.delegation import DelegationAnswer, delegate as delegate_goal, delegation as goal_state
from .bench.engines.probe import summary as engine_summary
from .bench.files import FileView, view
from .bench.models import Actor, Checkpoint, CheckSpec, NoteEntry
from .bench.results import CellResult, trimmed
from .bench.report_spec import ReportAnswer, ReportSpec
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
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
RUN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
STOP = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
KNOWN = (HubError, SlurmError, UnsupportedFile, ProjectError, KeyError, ValueError, OSError)
NoteKindArg = Literal["registration", "decision", "finding", "error", "incident", "note", "verdict"]
Disposition = Literal["active", "waiting", "complete", "blocked"]
Scope = Literal["twin", "full", "unknown"]


class DatasetsAnswer(Frozen):
    datasets: tuple[DatasetEntry, ...] = ()
    profile: dict[str, Any] | None = None
    twins: tuple[dict[str, Any], ...] = ()  # small stratified copies already built (bench.twin)


def _twins_of(path: str) -> tuple[dict[str, Any], ...]:
    from .bench.twins import twins_home

    home = twins_home(Path(path))
    found = []
    for info in sorted(home.glob("*/twin.json")) if home.is_dir() else []:
        try:
            data = json.loads(info.read_text())
        except (OSError, ValueError):
            continue
        found.append({"path": str(info.parent / "data.h5ad"), "cells": data.get("n_obs_twin"),
                      "stratify": data.get("stratify"), "keep": data.get("keep"), "fraction": data.get("fraction")})
    return tuple(found)


class SkillsAnswer(Frozen):
    skills: tuple[SkillInfo, ...] = ()
    text: str = ""


class ClusterAnswer(Frozen):
    workbench: str
    engines: tuple[str, ...] = ()  # the lab agents' engines: answering, paused, Claude's weekly window
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


MAX_INLINE_IMAGES = 3
MAX_INLINE_BYTES = 1_000_000
MAX_INLINE_PIXELS = 2000  # clients refuse larger images once a conversation holds many


def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG header, or None if it is not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def answer(result: CellResult, profile: ClientProfile, projects_dir: Path) -> CallToolResult:
    """The cell result as structured content, plus its figures for clients that show them to the model."""
    trimmed_result = trimmed(result, profile.max_output_chars)
    content: list[TextContent | ImageContent] = [TextContent(type="text", text=trimmed_result.model_dump_json())]
    if profile.inline_images:
        content += _images(trimmed_result, projects_dir)
    return CallToolResult(content=content, structured_content=trimmed_result.model_dump(mode="json"))


def _images(result: CellResult, projects_dir: Path) -> list[ImageContent]:
    project = result.ref.partition("#")[0]
    journal = (projects_dir / project / "journal").resolve()
    found = []
    for item in result.outputs:
        if not item.image or len(found) >= MAX_INLINE_IMAGES:
            continue
        path = (journal / item.image).resolve()
        try:
            if not path.is_relative_to(journal) or path.stat().st_size > MAX_INLINE_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        size = png_size(raw)
        if size is None or max(size) > MAX_INLINE_PIXELS:
            continue  # the figure stays in the journal and on the dashboard
        found.append(ImageContent(type="image", data=base64.b64encode(raw).decode(), mime_type="image/png"))
    return found


def _message(exc: BaseException) -> str:
    """KeyError('x') reads as 'x'; an OSError keeps its text (its first arg is only the errno)."""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


class Calls:
    """Audited tool calls: every call lands in logs/calls-*.jsonl; known errors become tool errors."""

    def __init__(self, hub: Hub) -> None:
        self.log_dir = hub.settings.logs_dir

    def __call__(self, tool: str, args: dict[str, Any], fn: Callable[[], T]) -> T:
        with audited(self.log_dir, tool, args):
            try:
                return fn()
            except KNOWN as exc:
                raise ToolError(_message(exc)) from exc

    async def threaded(self, tool: str, args: dict[str, Any], fn: Callable[[], T]) -> T:
        """For the calls that wait: the event loop stays free meanwhile."""
        return await anyio.to_thread.run_sync(lambda: self(tool, args, fn))


def register_bench_tools(mcp: MCPServer, hub: Hub, bench: BenchService) -> None:
    call = Calls(hub)
    _register_cells(mcp, hub, bench, call)
    _register_journal(mcp, bench, call)
    _register_reference(mcp, hub, bench, call)
    _register_delegation(mcp, hub, call)


def _register_cells(mcp: MCPServer, hub: Hub, bench: BenchService, call: Calls) -> None:
    acall = call.threaded

    @mcp.tool(annotations=READ)
    def projects() -> list[ProjectCard]:
        """Projects with their question, number of cells, where the work stands and the next action."""
        return call("projects", {}, bench.projects_list)

    @mcp.tool(annotations=WRITE)
    def create_project(project: str, question: str, datasets: list[str] | None = None) -> ProjectMeta:
        """Create a project ('name' or 'parent/child', lowercase) with its scientific question in one sentence."""
        return call("create_project", {"project": project}, lambda: bench.create_project(project, question, datasets or []))

    @mcp.tool(annotations=RUN)
    async def run(project: str, code: str, why: str, expect: str, setup: bool = False,
                  data_scope: Scope = "unknown", checks: list[CheckSpec] | None = None,
                  ctx: Context = None) -> CellResult:  # type: ignore[assignment]
        """Run a cell (Python; %%bash for shell; %%slurm to send it as a Slurm job) in the project's live
        kernel on a compute node. `why`: what the cell is for; `expect`: what you expect to see. `setup`: a
        cell to replay after a kernel restart. `data_scope`: twin or full data. `checks`: validations of
        what the cell produced (skills('checks')). Returns the outputs, or status "queued"/"running" with a
        ref: then call wait(ref), never run the same code again."""
        actor, profile = _client(ctx)
        args = {"project": project, "why": why[:200], "client": actor.client, "checks": len(checks or [])}
        result = await acall("run", args, lambda: bench.run(project, code, why, expect, checks=checks or (),
                                                             setup=setup, data_scope=data_scope, actor=actor,
                                                             wait_s=profile.run_wait_s))
        return answer(result, profile, hub.settings.projects_dir)  # type: ignore[return-value]

    @mcp.tool(annotations=READ)
    async def wait(ref: str, ctx: Context = None) -> CellResult:  # type: ignore[assignment]
        """Wait for a cell ('project#c0007') a little longer, and for the Slurm jobs it sent, and return its
        result or its state."""
        actor, profile = _client(ctx)
        result = await acall("wait", {"ref": ref, "client": actor.client},
                             lambda: bench.wait(ref, profile.run_wait_s))
        return answer(result, profile, hub.settings.projects_dir)  # type: ignore[return-value]



def _register_journal(mcp: MCPServer, bench: BenchService, call: Calls) -> None:
    @mcp.tool(annotations=READ)
    def journal(project: str, since: str | None = None, kinds: list[str] | None = None,
                limit: int = 20, ctx: Context = None) -> JournalView:  # type: ignore[assignment]
        """The project's hand-over, checkpoint and newest journal entries (cells and notes). Read it first in
        a new chat. `since`: the `newest` value of an earlier answer, to get only what is new. `left_out`: entries
        not shown to fit your limit (ask again with `since`, a smaller `limit` or `kinds`)."""
        _, profile = _client(ctx)
        return call("journal", {"project": project, "since": since},
                    lambda: bench.journal_view(project, since, kinds, limit, profile.max_output_chars + 4000))

    @mcp.tool(annotations=WRITE)
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

    @mcp.tool(annotations=WRITE)
    def handoff(project: str, text: str, disposition: Disposition, next_action: str = "",
                waiting_jobs: list[str] | None = None, ctx: Context = None) -> Checkpoint:  # type: ignore[assignment]
        """Before you stop: where the work stands (at most 120 lines; evidence stays in the journal), and the
        next action. `waiting`: the Slurm job ids the work waits on."""
        actor, _ = _client(ctx)
        return call("handoff", {"project": project, "disposition": disposition},
                    lambda: bench.handoff(project, text, disposition, next_action, waiting_jobs or [], actor))

    @mcp.tool(annotations=WRITE)
    def report(project: str, spec: ReportSpec | None = None, publish: bool = False,
               ctx: Context = None) -> ReportAnswer:  # type: ignore[assignment]
        """The project's report: a notebook that tells the study to a reader (skills('report_writing')). `spec`:
        title, summary, blocks — {"text": markdown}, {"cell": "c0012", "show": "all|outputs|code"},
        {"figure": "c0015"}, {"note": "n0007"} — and left_out ({ref: why it is not shown}). Code, outputs and
        figures come verbatim from the journal. Without `publish`: a draft (reports/draft) and its warnings,
        which are advice; with it: kept as reports/NN-<title>. Without `spec`: the published reports."""
        actor, _ = _client(ctx)
        return call("report", {"project": project, "publish": publish, "blocks": len(spec.blocks) if spec else 0},
                    lambda: bench.report(project, spec, publish, actor))



def _register_reference(mcp: MCPServer, hub: Hub, bench: BenchService, call: Calls) -> None:
    @mcp.tool(annotations=READ)
    def datasets(name: str | None = None) -> DatasetsAnswer:
        """Datasets in the shared library and the student's data/. With `name`: its metadata profile
        (counts or normalized, gene ids, species, obs columns with their levels)."""
        if name:
            def inspect() -> DatasetsAnswer:
                profile = hub.inspect(name)
                return DatasetsAnswer(profile=profile.model_dump(mode="json"), twins=_twins_of(profile.path))
            return call("datasets", {"name": name}, inspect)
        return call("datasets", {}, lambda: DatasetsAnswer(datasets=tuple(hub.datasets())))

    @mcp.tool(annotations=READ)
    def files(project: str | None = None, path: str = ".", max_chars: int = 20_000,
              ctx: Context = None) -> FileView:  # type: ignore[assignment]
        """List a folder or read a text file inside a project or the libraries (read-only; cells write).
        An .h5ad answers with its metadata profile."""
        _, profile = _client(ctx)
        ceiling = max(8_000, profile.max_output_chars * 4)  # a long log in pieces, not in one answer
        return call("files", {"project": project, "path": path},
                    lambda: view(hub.settings, project, path, max(100, min(max_chars, ceiling))))

    @mcp.tool(annotations=READ)
    def skills(name: str | None = None) -> SkillsAnswer:
        """Playbooks for kinds of work (resume, rigor, mbzuai_slurm...). Without `name`: the list."""
        if name:
            return call("skills", {"name": name}, lambda: SkillsAnswer(text=get_skill(name)))
        return call("skills", {}, lambda: SkillsAnswer(skills=tuple(list_skills())))

    @mcp.tool(annotations=READ)
    def cluster() -> ClusterAnswer:
        """The workbench, your job slots, quotas, limits, your jobs and the cluster's load."""
        def answer() -> ClusterAnswer:
            try:
                overview, problem = cached_overview(hub.settings), ""
            except (OSError, ValueError) as exc:
                overview, problem = None, f"overview unavailable: {exc}"
            return ClusterAnswer(workbench=bench.status(), engines=tuple(engine_summary(hub.settings)),
                                 overview=overview, problem=problem)
        return call("cluster", {}, answer)

    @mcp.tool(annotations=STOP)
    def stop(target: str) -> str:
        """Interrupt a running cell ('project#c0007'), cancel a job the bench sent (its job id), or
        'workbench' to stop the workbench now and free its job slot (variables are lost; files stay)."""
        return call("stop", {"target": target}, lambda: bench.stop(target))


def _register_delegation(mcp: MCPServer, hub: Hub, call: Calls) -> None:
    @mcp.tool(annotations=RUN)
    def delegate(project: str, objective: str, deliverables: str = "", max_turns: int = 20,
                 engine: Literal["auto", "claude", "codex"] = "auto", mode: Literal["free", "bench"] = "free",
                 report: bool = True, replace: bool = False,
                 ctx: Context = None) -> DelegationAnswer:  # type: ignore[assignment]
        """Hand a task to the lab agent on the cluster: it works on it alone, in Slurm slices about an hour
        apart, until it is done or its turns are spent. mode "free": its own shell, files and subagents in the
        project folder too (sandboxed); "bench": only these tools. Everything lands in the journal. First agree the
        task with the student (skills('delegating')): `objective` is the whole task in plain words, with what it
        needs to know; `deliverables` what must exist at the end; `max_turns` its budget (a turn: up to ~80 min).
        While it works on the project, a new task is refused unless replace=true."""
        actor, _ = _client(ctx)
        args = {"project": project, "max_turns": max_turns, "engine": engine, "mode": mode, "client": actor.client}
        return call("delegate", args, lambda: delegate_goal(hub.settings, hub.slurm, project, objective, deliverables,
                                                            max_turns, engine, mode, report, actor, replace))

    @mcp.tool(annotations=WRITE)  # (stop=true changes state)
    def delegation(project: str, stop: bool = False,
                   ctx: Context = None) -> DelegationAnswer:  # type: ignore[assignment]
        """Where the lab agent's work on a project stands: state, turns used, next action, queued slices, last
        events. stop=true stops it (queued slices exit at once; the journal keeps everything)."""
        actor, _ = _client(ctx)
        return call("delegation", {"project": project, "stop": stop},
                    lambda: goal_state(hub.settings, hub.slurm, project, stop, actor))
