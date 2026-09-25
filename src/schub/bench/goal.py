"""A lab agent's standing goal: projects/<p>/goal/goal.md and its state.

    ---
    engine: auto          # auto | claude | codex, always within the owner's engine policy
    max_turns: 30         # model turns before the goal stops as "blocked: budget"
    slice_minutes: 80     # one turn's time in its Slurm job
    pace_minutes: 60      # the next slice starts this long after the last one
    report: yes           # once complete, a writer turn publishes the study as a report notebook
    mode: free            # free: its own shell, file tools and subagents in the project folder (sandboxed,
                          # recorded in the journal each turn); bench: only the sc-hub tools
    ---
    The objective, in the student's words.

    goal/STOP                 the student's stop: the next slice ends the chain
    goal/report-only          a goal `schub goal-report` made for a chat project: reports, never research
    goal/state/owner.lock     one slice at a time (O_EXCL with a heartbeat: works across nodes)
    goal/state/sessions.json  each engine's session id, which engine took the last turn
    goal/state/turns.json     turns taken (the budget)
    goal/state/intent.json    a successor's comment before its sbatch (no blind resubmission)
    goal/state/events.jsonl   what each slice did
    goal/state/runs/<job>/    each turn's prompt, stdout, stderr and outcome
    goal/state/report.json    the report due after a completion: its status, the writer's turns and sessions

Progress lives in the journal: the hand-over (journal/handoff.md) and the checkpoint
(journal/checkpoint.json) are what a new session, or the other engine, starts from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..hashing import stable_hash
from .clock import stamp
from .fsio import read_json, write_json_atomic


def _yes_no(raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("yes", "true", "on"):
        return True
    if value in ("no", "false", "off"):
        return False
    raise ValueError(raw)


KEYS = {"engine": str, "max_turns": int, "slice_minutes": int, "pace_minutes": int, "report": _yes_no, "mode": str}
MODES = ("free", "bench")
LIMITS = {"max_turns": (1, 500), "slice_minutes": (20, 460), "pace_minutes": (5, 24 * 60)}
FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


class GoalError(ValueError):
    pass


@dataclass(frozen=True)
class GoalConfig:
    objective: str
    engine: str = "auto"
    max_turns: int = 30
    slice_minutes: int = 80
    pace_minutes: int = 60
    report: bool = True
    mode: str = "free"


def parse_goal(text: str) -> GoalConfig:
    match = FRONT.match(text.replace("\r\n", "\n"))
    header, objective = (match.group(1), match.group(2)) if match else ("", text)
    values: dict[str, object] = {}
    for line in header.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if not sep or key not in KEYS:
            raise GoalError(f"goal.md: unknown setting {key!r} (known: {', '.join(KEYS)})")
        try:
            values[key] = KEYS[key](raw)
        except ValueError as exc:
            kind = "yes or no" if key == "report" else "a whole number"
            raise GoalError(f"goal.md: {key} must be {kind}, got {raw!r}") from exc
    for key, (low, high) in LIMITS.items():
        if key in values and not low <= int(values[key]) <= high:  # type: ignore[arg-type]
            raise GoalError(f"goal.md: {key} must be from {low} to {high}")
    if values.get("engine", "auto") not in ("auto", "claude", "codex"):
        raise GoalError("goal.md: engine must be auto, claude or codex")
    if values.get("mode", "free") not in MODES:
        raise GoalError("goal.md: mode must be free or bench")
    objective = objective.strip()
    if not objective or len(objective) > 20_000:
        raise GoalError("goal.md: write the objective (at most 20000 characters) after the settings")
    return GoalConfig(objective=objective, **values)  # type: ignore[arg-type]


class Goal:
    def __init__(self, settings: Settings, project: str) -> None:
        self.settings = settings
        self.project = project
        self.project_dir = settings.projects_dir / project
        self.folder = self.project_dir / "goal"
        self.state = self.folder / "state"

    @property
    def job_name(self) -> str:
        """Unique per project: 'a/b' and 'a-b' share a slug, never the hash of the full path."""
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", self.project)[:48]
        return f"{self.settings.job_prefix}-goal-{slug}-{stable_hash(self.project, length=6)}"

    @property
    def job_names(self) -> tuple[str, str]:
        """This goal's slice names in the queue: the current one and the one before the hash was added."""
        return self.job_name, f"{self.settings.job_prefix}-goal-{re.sub(r'[^A-Za-z0-9_.-]', '-', self.project)[:60]}"

    def config(self) -> GoalConfig:
        try:
            return parse_goal((self.folder / "goal.md").read_text())
        except FileNotFoundError as exc:
            raise GoalError(f"{self.project} has no goal (goal/goal.md)") from exc

    def write(self, text: str) -> GoalConfig:
        config = parse_goal(text)
        self.state.mkdir(parents=True, exist_ok=True)
        temp = self.folder / ".goal.md.tmp"
        temp.write_text(text)
        temp.replace(self.folder / "goal.md")
        return config

    @property
    def report_only_marker(self) -> Path:
        return self.folder / "report-only"

    @property
    def report_only(self) -> bool:
        return self.report_only_marker.exists()

    def stopped(self) -> bool:
        return any(p.exists() for p in (self.folder / "STOP", self.project_dir / "STOP", self.settings.bench_dir / "STOP"))

    def event(self, kind: str, **values: object) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "events.jsonl").open("a") as handle:
            handle.write(json.dumps({"at": stamp(), "event": kind, **values}, default=str) + "\n")

    def events(self, limit: int = 20) -> list[dict]:
        try:
            lines = (self.state / "events.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []
        events = []
        for line in lines[-limit:]:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # a line torn by a concurrent append
        return events

    def sessions(self) -> dict:
        return read_json(self.state / "sessions.json") or {}

    def save_sessions(self, sessions: dict) -> None:
        write_json_atomic(self.state / "sessions.json", sessions)

    def archive_state(self) -> Path | None:
        """A new objective starts with a fresh budget and fresh sessions; the old ones are kept aside."""
        moved = [p for p in (self.state / "turns.json", self.state / "sessions.json") if p.exists()]
        if not moved:
            return None
        folder = self.state / "archive" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder.mkdir(parents=True, exist_ok=True)
        for path in moved:
            path.replace(folder / path.name)
        return folder

    def report_state(self) -> dict:
        return read_json(self.state / "report.json") or {}

    def save_report_state(self, state: dict) -> None:
        write_json_atomic(self.state / "report.json", state)

    def turns(self) -> int:
        return int((read_json(self.state / "turns.json") or {}).get("turns", 0))

    def count_turn(self, engine: str, job: str) -> int:
        turns = self.turns() + 1
        write_json_atomic(self.state / "turns.json", {"turns": turns, "last": {"engine": engine, "job": job,
                                                                             "at": stamp()}})
        return turns
