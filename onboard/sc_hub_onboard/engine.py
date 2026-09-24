"""The onboarding as a list of steps run one after another, with what the page shows.

Each step is idempotent: a step that is done is skipped when the helper runs again, so a
failure halfway (Wi-Fi, a typo in the login) is fixed and the run continues from there.
A step that needs the student (the password, a sign-in in the browser) asks through the
page and waits; nothing is ever asked in the assistant's chat.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

STATUSES = ("waiting", "running", "asking", "done", "skipped", "failed")
SECRET_FIELDS = ("password",)


class StepFailed(RuntimeError):
    """A step that cannot go on; `hint` says what the student can do."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class Skip(Exception):
    """The step does not apply here (e.g. no Claude Desktop on this computer)."""


@dataclass
class StepState:
    id: str
    title: str
    status: str = "waiting"
    detail: str = ""  # one line under the title: what is happening or what came out
    hint: str = ""  # what the student can do when it failed
    log: list[str] = field(default_factory=list)
    ask: dict[str, Any] | None = None  # a form the page shows: {"kind": ..., "fields": [...], ...}
    started: float = 0.0
    finished: float = 0.0


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    run: Callable[["Context"], str | None]  # returns the detail line to show when done
    weight: float = 1.0  # its share of the progress bar


class Context:
    """What a step can do: tell the page, ask the student, keep values for later steps."""

    def __init__(self, engine: "Engine", state: StepState) -> None:
        self.engine, self.state = engine, state

    @property
    def values(self) -> dict[str, Any]:
        return self.engine.values

    def say(self, line: str) -> None:
        self.state.detail = line
        self.log(line)

    def log(self, line: str) -> None:
        self.state.log = (self.state.log + [f"{time.strftime('%H:%M:%S')} {line}"])[-200:]
        self.engine.save()

    def ask(self, form: dict[str, Any]) -> dict[str, Any]:
        """Show `form` on the page and wait for the student's answer (secrets are kept only in memory)."""
        return self.engine.wait_for_answer(self.state, form)


class Engine:
    def __init__(self, steps: list[Step], state_path: Path, values: dict[str, Any] | None = None) -> None:
        self.steps = steps
        self.state_path = state_path
        self.values: dict[str, Any] = values or {}
        self.states = {s.id: StepState(id=s.id, title=s.title) for s in steps}
        self.finished = False
        self._answer: dict[str, Any] | None = None
        self._answered = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._load()

    # ---- persistence: done steps and the non-secret values survive a restart ----------------------

    def _load(self) -> None:
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.values.update({k: v for k, v in saved.get("values", {}).items() if k not in SECRET_FIELDS})
        for sid, data in saved.get("steps", {}).items():
            if sid in self.states and data.get("status") in ("done", "skipped"):
                state = self.states[sid]
                state.status, state.detail, state.log = data["status"], data.get("detail", ""), data.get("log", [])

    def save(self) -> None:
        with self._lock:
            data = {"values": {k: v for k, v in self.values.items() if k not in SECRET_FIELDS and _plain(v)},
                    "steps": {sid: {**asdict(s), "ask": None} for sid, s in self.states.items()}}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_name(self.state_path.name + ".tmp")
            temp.write_text(json.dumps(data, indent=1))
            temp.replace(self.state_path)

    # ---- running ----------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.finished = False
        self._thread = threading.Thread(target=self._run_all, name="onboard", daemon=True)
        self._thread.start()

    def _run_all(self) -> None:
        for step in self.steps:
            state = self.states[step.id]
            if state.status in ("done", "skipped"):
                continue
            if not self._run_one(step, state):
                return  # a failed step waits for the student (retry from the page)
        self.finished = True
        self.save()

    def _run_one(self, step: Step, state: StepState) -> bool:
        state.status, state.hint, state.started = "running", "", time.time()
        self.save()
        try:
            detail = step.run(Context(self, state))
            state.status, state.detail = "done", detail or state.detail
        except Skip as exc:
            state.status, state.detail = "skipped", str(exc)
        except StepFailed as exc:
            state.status, state.detail, state.hint = "failed", str(exc), exc.hint
        except Exception as exc:  # noqa: BLE001 - shown on the page; the student can retry or report it
            state.status, state.detail = "failed", f"{type(exc).__name__}: {exc}"
            state.hint = "Retry; if it fails again, send the log below to the pilot owner."
            state.log = state.log + traceback.format_exc().splitlines()[-6:]
        state.ask, state.finished = None, time.time()
        self.save()
        return state.status in ("done", "skipped")

    def retry(self, step_id: str | None = None) -> None:
        for state in self.states.values():
            if state.status == "failed" or state.id == step_id:
                state.status, state.hint = "waiting", ""
        self.start()

    # ---- asking the student ----------------------------------------------------------------------

    def wait_for_answer(self, state: StepState, form: dict[str, Any]) -> dict[str, Any]:
        self._answered.clear()
        state.status, state.ask = "asking", form
        self.save()
        self._answered.wait()
        answer, self._answer = self._answer or {}, None
        state.status, state.ask = "running", None
        self.save()
        if answer.get("cancel"):
            raise StepFailed("stopped on the page", "Press Retry to try this step again.")
        return answer

    def answer(self, values: dict[str, Any]) -> bool:
        if not any(s.status == "asking" for s in self.states.values()):
            return False
        self._answer = values
        self._answered.set()
        return True

    # ---- what the page shows -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        total = sum(s.weight for s in self.steps)
        done = sum(s.weight for s in self.steps if self.states[s.id].status in ("done", "skipped"))
        running = next((s for s in self.steps if self.states[s.id].status in ("running", "asking")), None)
        partial = running.weight * 0.3 if running else 0.0
        return {"progress": round(100 * (done + partial) / total), "finished": self.finished,
                "steps": [asdict(self.states[s.id]) for s in self.steps],
                "summary": self.values.get("summary", {})}


def _plain(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False
