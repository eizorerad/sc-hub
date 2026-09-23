"""The file queue between the MCP side (login node) and the runner (workbench job).

    bench/inbox/<ns>--<project slug>--<cid>.json    a cell to run
    bench/claimed/<same name>                       taken by the runner (atomic rename)
    bench/rejected/<same name>                      unreadable requests, with the reason
    bench/control/<project slug>--<cid>.<action>    e.g. interrupt a running cell

A file queue on Lustre instead of a socket: the MCP process lives only as long as
one ssh session, the token of the kernel never leaves the compute node, and the
login node only writes small JSON files.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .fsio import create_json_exclusive, read_json, write_json_atomic
from .models import CellRequest

CONTROL_ACTIONS = ("interrupt",)


def slug(project: str) -> str:
    """File-name form of a project path ('.' never occurs in project names)."""
    return project.replace("/", ".")


def unslug(name: str) -> str:
    return name.replace(".", "/")


@dataclass(frozen=True)
class Claimed:
    request: CellRequest
    path: Path  # the claimed file; removed once the cell is final


class Inbox:
    def __init__(self, bench_dir: Path) -> None:
        self.dir = bench_dir

    def _folder(self, name: str) -> Path:
        folder = self.dir / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def submit(self, request: CellRequest) -> Path:
        name = f"{time.time_ns():020d}--{slug(request.project)}--{request.cid}.json"
        path = self._folder("inbox") / name
        if not create_json_exclusive(path, request.model_dump(mode="json")):
            raise FileExistsError(f"request {name} already exists")
        return path

    def pending(self, project: str | None = None) -> list[CellRequest]:
        """Requests the runner has not taken yet, oldest first."""
        found = []
        for path in sorted(self._folder("inbox").glob("*.json")):
            if project is not None and f"--{slug(project)}--" not in path.name:
                continue
            request = _parse(read_json(path))
            if request is not None:
                found.append(request)
        return found

    def claim(self) -> list[Claimed]:
        """Take every waiting request, oldest first. Two runners never take the same one."""
        taken = []
        claimed = self._folder("claimed")
        for path in sorted(self._folder("inbox").glob("*.json")):
            target = claimed / path.name
            try:
                os.rename(path, target)
            except FileNotFoundError:
                continue
            request = _parse(read_json(target))
            if request is None:
                self._reject(target, "not a valid cell request")
                continue
            taken.append(Claimed(request=request, path=target))
        return taken

    def claimed(self) -> list[Claimed]:
        """Requests a runner took earlier (still here if it died before finishing them)."""
        found = []
        for path in sorted(self._folder("claimed").glob("*.json")):
            request = _parse(read_json(path))
            if request is not None:
                found.append(Claimed(request=request, path=path))
        return found

    def done(self, item: Claimed) -> None:
        item.path.unlink(missing_ok=True)

    def requeue(self, item: Claimed) -> None:
        """Give back a request that never started (the runner is stopping)."""
        try:
            os.rename(item.path, self._folder("inbox") / item.path.name)
        except FileNotFoundError:
            pass

    def reject(self, item: Claimed, reason: str) -> None:
        self._reject(item.path, reason)

    def rejected_reason(self, project: str, cid: str) -> str | None:
        for path in self._folder("rejected").glob(f"*--{slug(project)}--{cid}.reason.json"):
            data = read_json(path)
            if data is not None:
                return str(data.get("reason", ""))
        return None

    def _reject(self, path: Path, reason: str) -> None:
        target = self._folder("rejected") / path.name
        try:
            os.rename(path, target)
            write_json_atomic(target.with_suffix(".reason.json"), {"reason": reason})
        except OSError:
            path.unlink(missing_ok=True)

    # ---- controls ----------------------------------------------------------------

    def control(self, project: str, cid: str, action: str) -> bool:
        if action not in CONTROL_ACTIONS:
            raise ValueError(f"action must be one of {CONTROL_ACTIONS}")
        path = self._folder("control") / f"{slug(project)}--{cid}.{action}"
        return create_json_exclusive(path, {"action": action})

    def take_controls(self) -> list[tuple[str, str, str]]:
        """(project, cid, action) of every pending control; each is returned once."""
        found = []
        for path in sorted(self._folder("control").iterdir()):
            if path.name.startswith("."):
                continue  # a control still being written
            stem, _, action = path.name.rpartition(".")
            project_slug, _, cid = stem.partition("--")
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            if action in CONTROL_ACTIONS and cid:
                found.append((unslug(project_slug), cid, action))
        return found


def _parse(data: dict | None) -> CellRequest | None:
    if data is None:
        return None
    try:
        return CellRequest.model_validate(data)
    except ValidationError:
        return None
