"""MCP tools for many experiments: sweeps, sc-hub's submission queue, branch labels."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from mcp.server import MCPServer

from .labels import BranchLabel
from .queue import PumpResult, QueuedSubmission, QueueFailure, SubmitResult, submit_result
from .service import Hub
from .state import Frozen
from .sweeps import SweepResult

T = TypeVar("T")
Call = Callable[[str, dict[str, Any], Callable[[], T]], T]


class QueueStatus(Frozen):
    waiting: tuple[QueuedSubmission, ...]
    failed: tuple[QueueFailure, ...]
    pumped: PumpResult


def register_experiment_tools(mcp: MCPServer, hub: Hub, call: Call) -> None:
    @mcp.tool()
    def sweep_branch(project: str, branch: str, step: int, param: str, values: list[Any], sweep: str,
                     reason: str) -> SweepResult:
        """Try one parameter of one step over several values (2-24) as one experiment: one new
        branch per value, named <sweep>-<value>, sharing the steps before `step` (computed once).
        All variants are checked before any is saved. Submit them with submit_sweep."""
        args = {"project": project, "branch": branch, "step": step, "param": param, "values": values, "sweep": sweep}
        return call("sweep_branch", args, lambda: hub.sweep_branch(project, branch, step, param, values, sweep, reason))

    @mcp.tool()
    def submit_sweep(project: str, sweep: str) -> list[SubmitResult]:
        """Submit every branch of a sweep. Above the cap of active pipelines the rest waits in
        sc-hub's queue and is submitted automatically; do not resubmit queued plans."""
        return call("submit_sweep", {"project": project, "sweep": sweep},
                    lambda: [submit_result(o) for o in hub.submit_sweep(project, sweep)])

    @mcp.tool()
    def queue_status() -> QueueStatus:
        """Plans waiting in sc-hub's queue (submitted when a pipeline ends), and queued plans
        that could not be submitted (with the reason). Also submits what fits now."""
        def status() -> QueueStatus:
            pumped = hub.pump()
            return QueueStatus(waiting=tuple(hub.queued()), failed=tuple(hub.queue_failures()), pumped=pumped)
        return call("queue_status", {}, status)

    @mcp.tool()
    def cancel_queued(plan_id: str) -> bool:
        """Remove a plan from sc-hub's queue before it is submitted (True if it was waiting)."""
        return call("cancel_queued", {"plan_id": plan_id}, lambda: hub.cancel_queued(plan_id))

    @mcp.tool()
    def label_branch(project: str, branch: str, add_tags: list[str] | None = None, remove_tags: list[str] | None = None,
                     pinned: bool | None = None, archived: bool | None = None) -> BranchLabel:
        """Tag, pin or archive a branch (bookkeeping only: no new revision, nothing re-runs).
        Archived branches are hidden in the dashboard's Experiments table unless asked for."""
        args = {"project": project, "branch": branch, "add_tags": add_tags, "remove_tags": remove_tags,
                "pinned": pinned, "archived": archived}
        return call("label_branch", args, lambda: hub.label_branch(
            project, branch, add_tags or (), remove_tags or (), pinned, archived))
