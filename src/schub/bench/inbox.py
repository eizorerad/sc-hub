"""The file queue between the MCP side (login node) and the runner (workbench job).

    bench/inbox/<ns>--<project slug>--<cid>.json      a cell to run
    bench/claimed/<runner job>/<same name>            taken by that runner (atomic rename)
    bench/rejected/<same name>                        unreadable requests, with the reason
    bench/control/<project slug>--<cid>.<action>      e.g. interrupt a running cell

A file queue on Lustre instead of a socket: the MCP process lives only as long as
one ssh session, the token of the kernel never leaves the compute node, and the
login node only writes small JSON files. Each runner claims into its own folder,
so a new runner can tell what a dead one left behind from what a live one holds.
Project slugs may contain "--"; cids never do, so names are split from the right.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .fsio import create_json_exclusive, read_json, write_json_atomic
from .models import CID_PATTERN, PROJECT_PATTERN, CellRequest

CONTROL_ACTIONS = ("interrupt",)
OWNER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def slug(project: str) -> str:
    """File-name form of a project path ('.' never occurs in project names)."""
    return project.replace("/", ".")


def unslug(name: str) -> str:
    return name.replace(".", "/")


def _loads(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def split_name(stem: str) -> tuple[str, str]:
    """'<ns>--<slug>--<cid>' or '<slug>--<cid>' -> (project, cid)."""
    head, _, cid = stem.rpartition("--")
    if head[:1].isdigit() and "--" in head:
        prefix, _, rest = head.partition("--")
        if prefix.isdigit():
            head = rest
    return unslug(head), cid


def _check_owner(owner: str) -> str:
    if not OWNER.fullmatch(owner):
        raise ValueError(f"bad runner id {owner!r}")
    return owner


@dataclass(frozen=True)
class Claimed:
    request: CellRequest
    path: Path  # the claimed file; removed once the cell is final
    owner: str  # the runner (job id) that claimed it


class Inbox:
    def __init__(self, bench_dir: Path) -> None:
        self.dir = bench_dir

    def _folder(self, *parts: str) -> Path:
        folder = self.dir.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def submit(self, request: CellRequest) -> Path:
        name = f"{time.time_ns():020d}--{slug(request.project)}--{request.cid}.json"
        path = self._folder("inbox") / name
        if not create_json_exclusive(path, request.model_dump(mode="json")):
            raise FileExistsError(f"request {name} already exists")
        return path

    def pending(self, project: str | None = None) -> list[CellRequest]:
        """Requests no runner has taken yet, oldest first."""
        found = []
        for path in sorted(self._folder("inbox").glob("*.json")):
            request = _parse(read_json(path))
            if request is not None and (project is None or request.project == project):
                found.append(request)
        return found

    def claim(self, owner: str) -> list[Claimed]:
        """Take every waiting request, oldest first. Two runners never take the same one."""
        taken = []
        claimed = self._folder("claimed", _check_owner(owner))
        for path in sorted(self._folder("inbox").glob("*.json")):
            target = claimed / path.name
            try:
                os.rename(path, target)
            except FileNotFoundError:
                continue
            try:
                raw = target.read_bytes()
            except OSError:  # a passing file-server error: not an invalid request
                os.replace(target, path)
                continue
            request = _parse(_loads(raw))
            if request is None:
                if _young(target):  # written in place where link() is missing: finish writing first
                    os.replace(target, path)
                    continue
                self._reject(target, "not a valid cell request")
                continue
            taken.append(Claimed(request=request, path=target, owner=owner))
        return taken

    def owners(self) -> list[str]:
        base = self._folder("claimed")
        return sorted(p.name for p in base.iterdir() if p.is_dir() and OWNER.fullmatch(p.name))

    def claimed(self, owner: str | None = None) -> list[Claimed]:
        """Requests a runner took (still here if it died before finishing them)."""
        found = []
        for name in [_check_owner(owner)] if owner else self.owners():
            for path in sorted(self._folder("claimed", name).glob("*.json")):
                request = _parse(read_json(path))
                if request is not None:
                    found.append(Claimed(request=request, path=path, owner=name))
        return found

    def adopt(self, item: Claimed, owner: str) -> Claimed | None:
        """Take over a claim a dead runner left (rename into our own folder); None if someone else did."""
        target = self._folder("claimed", _check_owner(owner)) / item.path.name
        try:
            os.rename(item.path, target)
        except OSError:
            return None
        return Claimed(request=item.request, path=target, owner=owner)

    def done(self, item: Claimed) -> None:
        item.path.unlink(missing_ok=True)

    def requeue(self, item: Claimed) -> None:
        """Give back a request that never started (the runner is stopping)."""
        try:
            os.rename(item.path, self._folder("inbox") / item.path.name)
        except OSError:
            pass  # already gone: another runner handled it

    def reject(self, item: Claimed, reason: str) -> None:
        self._reject(item.path, reason)

    def rejected_reason(self, project: str, cid: str) -> str | None:
        for path in self._folder("rejected").glob("*.reason.json"):
            try:
                if split_name(path.name[: -len(".reason.json")]) == (project, cid):
                    data = read_json(path)
                    return str(data.get("reason", "")) if data else ""
            except ValueError:
                continue
        return None

    def unrecorded(self) -> list[tuple[str, str, str, Path]]:
        """Rejected requests whose reason the journal may not show yet: (project, cid, reason, reason file)."""
        found = []
        for path in sorted(self._folder("rejected").glob("*.reason.json")):
            try:
                project, cid = split_name(path.name[: -len(".reason.json")])
            except ValueError:
                continue
            data = read_json(path) or {}
            found.append((project, cid, str(data.get("reason", "")), path))
        return found

    def recorded(self, reason_file: Path) -> None:
        """The journal shows this rejection now: keep the file, out of unrecorded()."""
        try:
            os.rename(reason_file, reason_file.with_name(reason_file.name[: -len(".reason.json")] + ".recorded.json"))
        except OSError:
            pass

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
        if not re.fullmatch(PROJECT_PATTERN, project) or not re.fullmatch(CID_PATTERN, cid):
            raise ValueError(f"bad cell reference {project}#{cid}")
        folder = self._folder("control")
        if any(split_name(p.name.rpartition(".")[0]) == (project, cid) and p.name.endswith(f".{action}")
               for p in folder.iterdir() if not p.name.startswith(".")):
            return False  # already pending
        # the same numeric prefix as inbox names, so a project named like '2024--pbmc' parses back whole
        path = folder / f"{time.time_ns():020d}--{slug(project)}--{cid}.{action}"
        return create_json_exclusive(path, {"action": action})

    def take_controls(self) -> list[tuple[str, str, str]]:
        """(project, cid, action) of every pending control; each is returned once."""
        found = []
        for path in sorted(self._folder("control").iterdir()):
            if path.name.startswith(".") or not path.is_file():
                continue  # a control still being written, or something that is not ours
            stem, _, action = path.name.rpartition(".")
            project, cid = split_name(stem)
            try:
                path.unlink()
            except OSError:
                continue
            if action in CONTROL_ACTIONS and re.fullmatch(CID_PATTERN, cid):
                found.append((project, cid, action))
        return found


YOUNG_S = 10.0


def _young(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < YOUNG_S
    except OSError:
        return False


def _parse(data: dict | None) -> CellRequest | None:
    if data is None:
        return None
    try:
        return CellRequest.model_validate(data)
    except ValidationError:
        return None
