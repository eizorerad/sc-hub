"""Run one cell on a project kernel and collect what it produces.

The loop ends on the kernel's `idle` status for the cell. If that message is lost
(the iopub buffer can drop messages under heavy output) the execute_reply on the
shell channel ends it too, after a short grace period. A cell that ignores the
interrupt while the workbench is retiring is stopped by shutting the kernel down.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .kernels import ProjectKernel
from .ledger import DRAIN_EXPRESSION
from .outputs import OutputCollector

Status = Literal["ok", "error", "interrupted", "lost"]
REPLY_TIMEOUT_S = 30.0
IDLE_GRACE_S = 2.0
KILL_GRACE_S = 10.0


@dataclass(frozen=True)
class ExecResult:
    status: Status
    user_expressions: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False


@dataclass
class _Run:
    msg_id: str
    interrupted_at: float | None = None
    reply: dict[str, Any] | None = None
    reply_at: float = 0.0


def execute(
    kernel: ProjectKernel,
    code: str,
    collector: OutputCollector,
    *,
    should_interrupt: Callable[[], bool],
    on_progress: Callable[[], None],
    poll_s: float,
    should_kill: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
) -> ExecResult:
    client = kernel.client
    if client is None:
        return ExecResult(status="lost")
    run = _Run(msg_id=client.execute(code, store_history=True, allow_stdin=False, stop_on_error=True))
    while True:
        stopped = _steer(kernel, run, should_interrupt, should_kill, clock)
        if stopped is not None:
            return stopped
        try:
            msg = client.get_iopub_msg(timeout=poll_s)
        except queue.Empty:
            if not kernel.alive():
                return ExecResult(status="lost", interrupted=run.interrupted_at is not None)
            if run.reply is None:
                run.reply, run.reply_at = _poll_reply(kernel, run.msg_id), clock()
            elif clock() - run.reply_at > IDLE_GRACE_S:
                break  # the reply came, iopub is drained, the idle status never did
            on_progress()
            continue
        if msg.get("parent_header", {}).get("msg_id") != run.msg_id:
            continue
        kind, content = msg.get("msg_type", ""), msg.get("content", {})
        if kind == "status" and content.get("execution_state") == "idle":
            break
        collector.add(kind, content)
        on_progress()
    reply = run.reply or _shell_reply(kernel, run.msg_id)
    return _status(kernel, reply, run.interrupted_at is not None, collector)


def _steer(kernel: ProjectKernel, run: _Run, should_interrupt: Callable[[], bool],
           should_kill: Callable[[], bool], clock: Callable[[], float]) -> ExecResult | None:
    if run.interrupted_at is None and should_interrupt():
        try:
            kernel.interrupt()
        except Exception:  # noqa: BLE001 - a dead kernel is found by alive() below
            pass
        run.interrupted_at = clock()
    if run.interrupted_at is not None and should_kill() and clock() - run.interrupted_at > KILL_GRACE_S:
        kernel.shutdown()
        return ExecResult(status="lost", interrupted=True)
    return None


def _status(kernel: ProjectKernel, reply: dict[str, Any] | None, interrupted: bool,
            collector: OutputCollector) -> ExecResult:
    if reply is None:
        status: Status = "lost" if not kernel.alive() else ("interrupted" if interrupted else "error")
        return ExecResult(status=status, interrupted=interrupted)
    content = reply.get("content", {})
    expressions = content.get("user_expressions") or {}
    if content.get("status") == "ok":
        return ExecResult(status="ok", user_expressions=expressions, interrupted=interrupted)
    error = collector.error()
    stopped = interrupted or (error is not None and error[0] == "KeyboardInterrupt")
    return ExecResult(status="interrupted" if stopped else "error", user_expressions=expressions,
                      interrupted=interrupted)


def _poll_reply(kernel: ProjectKernel, msg_id: str) -> dict[str, Any] | None:
    client = kernel.client
    if client is None:
        return None
    try:
        reply = client.get_shell_msg(timeout=0)
    except queue.Empty:
        return None
    return reply if reply.get("parent_header", {}).get("msg_id") == msg_id else None


def _shell_reply(kernel: ProjectKernel, msg_id: str, timeout_s: float = REPLY_TIMEOUT_S) -> dict[str, Any] | None:
    client = kernel.client
    deadline = time.monotonic() + timeout_s
    while client is not None and time.monotonic() < deadline:
        try:
            reply = client.get_shell_msg(timeout=1.0)
        except queue.Empty:
            if not kernel.alive():
                return None
            continue
        if reply.get("parent_header", {}).get("msg_id") == msg_id:
            return reply
    return None


def drain_ledger(kernel: ProjectKernel, timeout_s: float = 10.0) -> dict[str, Any] | None:
    """The events helpers recorded in the kernel during the last cell, whatever its outcome
    (ipykernel skips user_expressions when a cell fails, so they are asked for separately)."""
    client = kernel.client
    if client is None or not kernel.alive():
        return None
    try:
        msg_id = client.execute("", silent=True, store_history=False, allow_stdin=False,
                                user_expressions={"ledger": DRAIN_EXPRESSION})
    except Exception:  # noqa: BLE001 - no ledger is better than a stuck runner
        return None
    reply = _shell_reply(kernel, msg_id, timeout_s)
    expressions = (reply or {}).get("content", {}).get("user_expressions") or {}
    return expressions.get("ledger")


PRIME = ("get_ipython().run_line_magic('load_ext', 'schub.bench.magics')\n"
         "import schub.bench.kernel_api as bench\n"
         "__import__('schub.bench.kernel_api', fromlist=['alias']).alias()")


def silent(kernel: ProjectKernel, code: str, timeout_s: float = 30.0) -> dict[str, Any] | None:
    """Run bookkeeping code in the kernel without output or history; the reply's content."""
    client = kernel.client
    if client is None or not kernel.alive():
        return None
    try:
        msg_id = client.execute(code, silent=True, store_history=False, allow_stdin=False)
    except Exception:  # noqa: BLE001 - bookkeeping must never break the cell
        return None
    reply = _shell_reply(kernel, msg_id, timeout_s)
    return (reply or {}).get("content")


def prime(kernel: ProjectKernel) -> bool:
    """%%slurm and `bench` in a fresh kernel. False if sc-hub is not importable there."""
    content = silent(kernel, PRIME, timeout_s=60.0)
    return bool(content and content.get("status") == "ok")


def announce(kernel: ProjectKernel, ref: str, checks: list[dict[str, Any]]) -> None:
    """Tell the kernel which cell runs next (a %%slurm job then belongs to it)."""
    import json

    silent(kernel, f"__import__('schub.bench.kernel_api', fromlist=['set_cell']).set_cell({ref!r}, "
                   f"{json.dumps(checks)!r})")
