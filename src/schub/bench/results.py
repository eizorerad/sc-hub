"""What a tool call returns about a cell: its state, outputs, files, jobs, checks,
and a plain hint about what to do next."""

from __future__ import annotations

from typing import Literal

from ..state import Frozen
from .models import CellEntry, CheckResult, Download, FileChange, JobRef, OutputItem

ResultStatus = Literal["queued", "rejected", "running", "ok", "error", "interrupted", "lost", "retired"]


class CellResult(Frozen):
    ref: str
    status: ResultStatus
    outputs: tuple[OutputItem, ...] = ()
    files: tuple[FileChange, ...] = ()
    downloads: tuple[Download, ...] = ()
    jobs: tuple[JobRef, ...] = ()
    checks: tuple[CheckResult, ...] = ()
    duration_s: float | None = None
    kernel_epoch: str = ""
    kernel_restarted: bool = False
    message: str = ""
    workbench: str = ""
    hint: str = ""


HINTS = {
    "queued": "The cell waits for the workbench. Call wait(ref) in a minute; do not submit it again.",
    "running": "Still running. Call wait(ref) again, or interrupt it with stop(ref).",
    "ok": "",
    "error": "The cell raised an error (see outputs). Fix the code and run a new cell.",
    "interrupted": "The cell was interrupted before it finished.",
    "lost": "The kernel or the workbench stopped during this cell: variables are gone. Re-run setup cells, then this one.",
    "retired": "The workbench stopped during this cell (time limit or stop). Re-run setup cells, then this one.",
    "rejected": "The request was refused; see the message.",
}


def waiting_result(ref: str, status: ResultStatus, workbench: str) -> CellResult:
    message = workbench if status == "rejected" else ""
    return CellResult(ref=ref, status=status, workbench="" if status == "rejected" else workbench,
                      message=message, hint=HINTS[status])


def cell_result(entry: CellEntry, previous_epoch: str, workbench: str, setup_refs: tuple[str, ...]) -> CellResult:
    restarted = bool(previous_epoch) and bool(entry.kernel_epoch) and previous_epoch != entry.kernel_epoch
    hint = HINTS[entry.status]
    if restarted:
        replay = f" Setup cells to run again: {', '.join(setup_refs)}." if setup_refs else ""
        hint = (f"The kernel restarted before this cell: variables from earlier cells are gone.{replay} "
                + hint).strip()
    failed = [c.name for c in entry.check_results if c.status in ("fail", "error")]
    if failed:
        hint = f"Checks failed: {', '.join(failed)}. Do not build on this result until they pass. {hint}".strip()
    return CellResult(
        ref=entry.ref, status=entry.status, outputs=entry.outputs, files=entry.files, downloads=entry.downloads,
        jobs=entry.jobs, checks=entry.check_results, duration_s=entry.duration_s, kernel_epoch=entry.kernel_epoch,
        kernel_restarted=restarted, message=entry.message, workbench=workbench, hint=hint,
    )


def trimmed(result: CellResult, max_chars: int) -> CellResult:
    """The same result with output text cut to what a client takes well (the journal keeps more)."""
    total = sum(len(o.text) for o in result.outputs)
    if total <= max_chars:
        return result
    budget = max(200, max_chars // max(1, len(result.outputs)))
    outputs = []
    for item in result.outputs:
        if len(item.text) <= budget:
            outputs.append(item)
            continue
        cut = len(item.text) - budget
        text = item.text[: budget * 2 // 3] + f"\n[... {cut} characters; the journal has more ...]\n" + item.text[-budget // 3:]
        outputs.append(item.model_copy(update={"text": text, "truncated": item.truncated + cut}))
    return result.model_copy(update={"outputs": tuple(outputs)})
