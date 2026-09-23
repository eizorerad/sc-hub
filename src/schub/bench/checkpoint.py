"""Hand-over between sessions: where the work stands and what comes next.

    projects/<p>/journal/checkpoint.json   disposition, next action, jobs waited on
    projects/<p>/journal/handoff.md        at most 120 lines; a new session reads it first

Evidence belongs in the journal; the hand-over only says where to pick up (the
VCC2026 rule: keep it under 120 lines).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from .clock import Clock, stamp
from .fsio import fsync_dir, read_json, write_json_atomic
from .models import Actor, Checkpoint, WaitingJob

MAX_HANDOFF_LINES = 120
MAX_HANDOFF_CHARS = 16_000


class CheckpointError(ValueError):
    pass


def check_handoff(text: str) -> list[str]:
    lines = text.strip().splitlines()
    if not lines:
        raise CheckpointError("the hand-over is empty")
    if len(lines) > MAX_HANDOFF_LINES or len(text) > MAX_HANDOFF_CHARS:
        raise CheckpointError(f"keep the hand-over under {MAX_HANDOFF_LINES} lines; evidence belongs in the journal")
    return lines


class CheckpointStore:
    def __init__(self, project_dir: Path, now: Clock = stamp) -> None:
        self.folder = project_dir / "journal"
        self.now = now

    @property
    def path(self) -> Path:
        return self.folder / "checkpoint.json"

    @property
    def handoff_path(self) -> Path:
        return self.folder / "handoff.md"

    def read(self) -> Checkpoint:
        data = read_json(self.path)
        try:
            return Checkpoint.model_validate(data) if data is not None else Checkpoint()
        except ValidationError:
            return Checkpoint(reason="checkpoint.json was unreadable; treat the work as active")

    def write(
        self,
        disposition: str,
        next_action: str = "",
        waiting_jobs: Sequence[WaitingJob] = (),
        reason: str = "",
        actor: Actor | None = None,
    ) -> Checkpoint:
        if disposition == "waiting" and not waiting_jobs:
            raise CheckpointError("'waiting' needs the jobs it waits on")
        if disposition in ("active", "waiting") and not next_action.strip():
            raise CheckpointError(f"'{disposition}' needs a next action")
        try:
            checkpoint = Checkpoint(
                disposition=disposition, next_action=next_action.strip(), waiting_jobs=tuple(waiting_jobs),
                reason=reason.strip(), updated=self.now(), actor=actor or Actor(),
            )
        except ValidationError as exc:
            raise CheckpointError(str(exc.errors()[0]["msg"])) from exc
        write_json_atomic(self.path, checkpoint.model_dump(mode="json"))
        return checkpoint

    def read_handoff(self) -> str:
        try:
            return self.handoff_path.read_text()
        except OSError:
            return ""

    def write_handoff(self, text: str) -> None:
        lines = check_handoff(text)
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.handoff_path.with_name(".handoff.md.tmp")
        temp.write_text("\n".join(lines) + "\n")
        temp.replace(self.handoff_path)
        fsync_dir(self.folder)
