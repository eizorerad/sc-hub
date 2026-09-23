"""Records of the bench journal. All frozen: a change is a new record or an addendum."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from ..state import Frozen

CellStatus = Literal["queued", "running", "ok", "error", "interrupted", "lost", "retired"]
FINAL_STATUSES: frozenset[str] = frozenset({"ok", "error", "interrupted", "lost", "retired"})
NoteKind = Literal["registration", "decision", "finding", "error", "incident", "note", "verdict", "handoff"]
NOTE_KINDS: tuple[str, ...] = ("registration", "decision", "finding", "error", "incident", "note", "verdict", "handoff")
Audience = Literal["human", "agent", "both"]
DataScope = Literal["twin", "full", "unknown"]
MAX_CODE_CHARS = 200_000
MAX_TEXT_CHARS = 4_000
# The same rules as project names (projects.SEGMENT, at most 3 levels) and journal ids:
# they become file names, so nothing else may pass.
PROJECT_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,40}(/[a-z0-9][a-z0-9_-]{0,40}){0,2}$"
CID_PATTERN = r"^c\d{4,9}$"
NID_PATTERN = r"^n\d{4,9}$"
Short = Field(default="", max_length=200)


class Actor(Frozen):
    """Who made a record. Filled by the server (MCP client info, lab agent), never by the agent."""

    kind: Literal["chat", "lab_agent", "human", "system"] = "chat"
    client: str = Short
    client_version: str = Short
    engine: str = Short
    model: str = Short
    effort: str = Short
    session_id: str = Short


class CheckSpec(Frozen):
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


def _required_text(value: str, what: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{what} must say something")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"{what} is longer than {MAX_TEXT_CHARS} characters")
    return text


class CellRequest(Frozen):
    project: str = Field(pattern=PROJECT_PATTERN)
    cid: str = Field(pattern=CID_PATTERN)
    code: str = Field(max_length=MAX_CODE_CHARS)
    why: str
    expect: str
    checks: tuple[CheckSpec, ...] = ()
    setup: bool = False  # replayed after a kernel restart
    data_scope: DataScope = "unknown"
    actor: Actor = Field(default_factory=Actor)
    created: str

    @field_validator("why")
    @classmethod
    def _why(cls, value: str) -> str:
        return _required_text(value, "why")

    @field_validator("expect")
    @classmethod
    def _expect(cls, value: str) -> str:
        return _required_text(value, "expect")

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code is empty")
        return value


class OutputItem(Frozen):
    kind: Literal["stream", "result", "display", "error"]
    name: str = ""  # stdout / stderr for streams
    text: str = ""
    image: str = ""  # path relative to the journal folder
    ename: str = ""
    evalue: str = ""
    truncated: int = 0  # characters left out of `text`


class FileChange(Frozen):
    path: str  # relative to the project folder
    change: Literal["created", "modified", "deleted"]
    size: int | None = None
    sha256: str | None = None


class Download(Frozen):
    url: str
    path: str
    size: int
    sha256: str
    status: Literal["ok", "failed"] = "ok"
    message: str = ""


class JobRef(Frozen):
    job_id: str
    name: str = ""
    partition: str = ""
    comment: str = ""
    state: str = "SUBMITTED"
    submitted: str = ""
    finished: str = ""
    exit_code: int | None = None
    log: str = ""


class CheckResult(Frozen):
    name: str
    status: Literal["pass", "fail", "error", "skipped"]
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class CellEntry(Frozen):
    kind: Literal["cell"] = "cell"
    ref: str
    project: str = Field(pattern=PROJECT_PATTERN)
    cid: str = Field(pattern=CID_PATTERN)
    why: str
    expect: str
    code: str
    checks: tuple[CheckSpec, ...] = ()
    setup: bool = False
    data_scope: DataScope = "unknown"
    actor: Actor = Field(default_factory=Actor)
    created: str
    started: str | None = None
    finished: str | None = None
    status: CellStatus = "queued"
    kernel_epoch: str = ""
    outputs: tuple[OutputItem, ...] = ()
    files: tuple[FileChange, ...] = ()
    files_truncated: bool = False
    downloads: tuple[Download, ...] = ()
    jobs: tuple[JobRef, ...] = ()
    check_results: tuple[CheckResult, ...] = ()
    duration_s: float | None = None
    message: str = ""  # why a cell is lost / retired, in plain words

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES


class NoteEntry(Frozen):
    kind: NoteKind
    ref: str
    project: str = Field(pattern=PROJECT_PATTERN)
    nid: str = Field(pattern=NID_PATTERN)
    text: str
    because: tuple[str, ...] = ()  # refs of the cells (or notes) this rests on
    reverses_if: str = ""
    verdict: Literal["registered", "descriptive", "invalid"] | None = None
    audience: Audience = "both"
    actor: Actor = Field(default_factory=Actor)
    created: str
    unresolved_numbers: tuple[str, ...] = ()  # numbers in `text` not found in the cited cells


class WaitingJob(Frozen):
    job_id: str
    name: str = ""
    cluster: str = "mbzuai"


class Checkpoint(Frozen):
    disposition: Literal["active", "waiting", "complete", "blocked"] = "active"
    next_action: str = ""
    waiting_jobs: tuple[WaitingJob, ...] = ()
    reason: str = ""
    updated: str = ""
    actor: Actor = Field(default_factory=Actor)
