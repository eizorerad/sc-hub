"""Run one cell on a project kernel and collect what it produces."""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .kernels import ProjectKernel
from .outputs import OutputCollector

Status = Literal["ok", "error", "interrupted", "lost"]
REPLY_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ExecResult:
    status: Status
    user_expressions: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False


def execute(
    kernel: ProjectKernel,
    code: str,
    collector: OutputCollector,
    *,
    should_interrupt: Callable[[], bool],
    on_progress: Callable[[], None],
    poll_s: float,
    user_expressions: dict[str, str] | None = None,
) -> ExecResult:
    client = kernel.client
    if client is None:
        return ExecResult(status="lost")
    msg_id = client.execute(code, store_history=True, allow_stdin=False, stop_on_error=True,
                            user_expressions=user_expressions or {})
    interrupt_sent = False
    while True:
        if not interrupt_sent and should_interrupt():
            kernel.interrupt()
            interrupt_sent = True
        try:
            msg = client.get_iopub_msg(timeout=poll_s)
        except queue.Empty:
            if not kernel.alive():
                return ExecResult(status="lost", interrupted=interrupt_sent)
            on_progress()
            continue
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        kind, content = msg.get("msg_type", ""), msg.get("content", {})
        if kind == "status" and content.get("execution_state") == "idle":
            break
        collector.add(kind, content)
        on_progress()
    return _finish(kernel, msg_id, interrupt_sent, collector)


def _finish(kernel: ProjectKernel, msg_id: str, interrupt_sent: bool, collector: OutputCollector) -> ExecResult:
    reply = _shell_reply(kernel, msg_id)
    if reply is None:
        status: Status = "lost" if not kernel.alive() else ("interrupted" if interrupt_sent else "error")
        return ExecResult(status=status, interrupted=interrupt_sent)
    content = reply.get("content", {})
    expressions = content.get("user_expressions") or {}
    if content.get("status") == "ok":
        return ExecResult(status="ok", user_expressions=expressions, interrupted=interrupt_sent)
    error = collector.error()
    stopped = interrupt_sent or (error is not None and error[0] == "KeyboardInterrupt")
    return ExecResult(status="interrupted" if stopped else "error", user_expressions=expressions,
                      interrupted=interrupt_sent)


def _shell_reply(kernel: ProjectKernel, msg_id: str) -> dict[str, Any] | None:
    client = kernel.client
    deadline = time.monotonic() + REPLY_TIMEOUT_S
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
