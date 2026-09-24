"""The onboarding as a list of steps run one after another, with what the page shows.

Each step is idempotent: a step that is done is skipped when the helper runs again, so a
failure halfway (Wi-Fi, a typo in the login) is fixed and the run continues from there.
A step that needs the student (the password, a sign-in in the browser) asks through the
page and waits; nothing is ever asked in the assistant's chat.
"""

from __future__ import annotations

import itertools
import json
import os
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

    def show(self, form: dict[str, Any]) -> None:
        """Show `form` (e.g. a link and a code) while the step keeps working; its buttons are `choices` only."""
        self.engine.show(self.state, form)

    def poll(self) -> dict[str, Any] | None:
        """The student's answer to a shown form, if one came (a choice, or {"cancel": True})."""
        return self.engine.take_answer()

    def clear(self) -> None:
        self.engine.set(self.state, status="running", ask=None)


class Engine:
    def __init__(self, steps: list[Step], state_path: Path, values: dict[str, Any] | None = None) -> None:
        self.steps = steps
        self.state_path = state_path
        self.values: dict[str, Any] = values or {}
        self.states = {s.id: StepState(id=s.id, title=s.title) for s in steps}
        self.finished = False
        self._answer: dict[str, Any] | None = None
        self._answered = threading.Event()
        self._lock = threading.Lock()  # the step thread and the page's requests both change the states
        self._form_ids = itertools.count(1)
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
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.state_path.parent, 0o700)  # the login and the steps' logs: this account only
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

    def set(self, state: StepState, **fields: Any) -> None:
        """Change a step's state; every change of status or form goes through here, under the lock."""
        with self._lock:
            for name, value in fields.items():
                setattr(state, name, value)
        self.save()

    def _run_one(self, step: Step, state: StepState) -> bool:
        self.set(state, status="running", hint="", started=time.time())
        end: dict[str, Any]
        try:
            detail = step.run(Context(self, state))
            end = {"status": "done", "detail": detail or state.detail}
        except Skip as exc:
            end = {"status": "skipped", "detail": str(exc)}
        except StepFailed as exc:
            end = {"status": "failed", "detail": str(exc), "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001 - shown on the page; the student can retry or report it
            end = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}",
                   "hint": "Retry; if it fails again, send the log below to the pilot owner.",
                   "log": state.log + traceback.format_exc().splitlines()[-6:]}
        self.set(state, **end, ask=None, finished=time.time())
        return state.status in ("done", "skipped")

    def retry(self, step_id: str | None = None) -> None:
        with self._lock:
            for state in self.states.values():
                if state.status == "failed" or state.id == step_id:
                    state.status, state.hint = "waiting", ""
        self.start()

    # ---- asking the student ----------------------------------------------------------------------

    def wait_for_answer(self, state: StepState, form: dict[str, Any]) -> dict[str, Any]:
        self._answered.clear()
        self.set(state, status="asking", ask={**form, "id": str(next(self._form_ids))})
        self._answered.wait()
        answer, self._answer = self._answer or {}, None
        self.set(state, status="running", ask=None)
        if answer.get("cancel"):
            raise StepFailed("stopped on the page", "Press Retry to try this step again.")
        return answer

    def show(self, state: StepState, form: dict[str, Any]) -> None:
        self._answered.clear()
        self._answer = None
        self.set(state, status="asking", ask={**form, "wait": True, "id": str(next(self._form_ids))})

    def take_answer(self) -> dict[str, Any] | None:
        if not self._answered.is_set():
            return None
        self._answered.clear()
        answer, self._answer = self._answer or {}, None
        return answer

    def answer(self, values: dict[str, Any]) -> bool:
        """Take an answer to the form on the page now (by its id), if it answers what that form asks."""
        with self._lock:
            state = next((s for s in self.states.values() if s.status == "asking" and s.ask
                          and s.ask.get("id") == values.get("form_id")), None)
            if state is None or not _answers(state.ask, values):
                return False
            self._answer = {k: v for k, v in values.items() if k != "form_id"}
            state.status, state.ask = "running", None  # the form goes away at once, not at the step's next look
            self._answered.set()
        self.save()
        return True

    def _states(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(self.states[s.id]) for s in self.steps]

    # ---- what the page shows -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        total = sum(s.weight for s in self.steps)
        done = sum(s.weight for s in self.steps if self.states[s.id].status in ("done", "skipped"))
        running = next((s for s in self.steps if self.states[s.id].status in ("running", "asking")), None)
        partial = running.weight * 0.3 if running else 0.0
        return {"progress": round(100 * (done + partial) / total), "finished": self.finished,
                "steps": self._states(),
                "summary": self.values.get("summary", {})}


def _answers(form: dict[str, Any], values: dict[str, Any]) -> bool:
    """A form's own buttons only: Stop (unless it has none), one of its choices, or its required fields filled
    (a waiting form has no fields to send)."""
    if values.get("cancel"):
        return form.get("cancel") is not False
    if "choice" in values:
        return values["choice"] in {c.get("name") for c in form.get("choices", [])}
    if form.get("wait"):
        return False
    return all(values.get(f["name"]) not in (None, "", False) for f in form.get("fields", []) if f.get("required"))


def _plain(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False
