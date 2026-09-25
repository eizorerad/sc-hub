"""The lab agent: a standing goal worked on in Slurm slices by Claude Code or Codex
(the VCC2026 goal-agent pattern, through the bench's own MCP tools and journal).

    python -m schub.bench.goal_agent --project P --run     # inside a slice job

Each slice, in order: the STOP files and a finished checkpoint end the chain; one
slice holds the goal (an O_EXCL lock file with a heartbeat); it arms its successor FIRST (afterany:self,
--begin=now+pace), with the intent written before sbatch so an ambiguous failure is
recovered by comment, never resubmitted blind; while the checkpoint waits on Slurm
jobs that are still queued or running it ends without calling a model; then the
turn budget, the owner's weekly ceiling, the engine policy and the pauses pick an
engine, and one turn runs, resuming that engine's session. A new session (another
engine, or a lost one) starts from the journal's hand-over, not someone else's chat.
The engine runs through bench/guard/<engine> with only the sc-hub MCP server, so its
cells land in the journal like a student's chat agent's, marked lab_agent.

When a research turn hands the work over as complete (and goal.md keeps `report: yes`),
the report is due: a writer turn, a fresh session that reads only the journal, builds
the report notebook with the `report` tool (at most REPORT_TURNS turns; it cannot
change the hand-over). The goal then ends whether or not the report was published.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import secrets
import signal
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping

from ..bricks import Resources
from ..config import Settings, load_settings
from ..locking import LockTimeout, exclusive
from ..projects import ProjectError, ProjectStore
from ..slurm import JobSpec, Slurm, SlurmError, render_script
from .checkpoint import CheckpointStore
from .engines.base import (LABELS, SIGN_IN_HOW, Engine, McpServer, Outcome, Turn, credential_fingerprint,
                           install_guards, sign_in_fingerprint)
from .engines.claude import Claude
from .engines.codex import Codex
from .engines import window
from .engines.cooldown import Cooldown, reset_hint
from .engines.policy import PolicyError, load as load_policy, order
from .fsio import read_json, write_json_atomic
from .goal import Goal, GoalConfig, GoalError, parse_goal
from .inbox import Inbox
from .journal import Journal
from .clock import stamp
from .models import Actor, Checkpoint
from .report_store import ReportStore
from .workbench import PASS_THROUGH, Workbench

ADAPTERS: dict[str, Engine] = {"claude": Claude(), "codex": Codex()}
TERMINAL = ("complete", "blocked")
# Slurm will never start a job waiting for one of these: the model must hear of it, not wait forever
# (JobHeldUser is left out: a hold the student placed on purpose)
STUCK = re.compile(r"JobHeldAdmin|requeued.?held|held.?state|DependencyNeverSatisfied|BadConstraints|PartitionConfig|"
                   r"PartitionTimeLimit|QOSMaxWallDurationPerJobLimit|Invalid(?:Account|QOS)|launch.?failed", re.I)
MIN_TURN_S = 600  # the other engine (or a new session) takes over only with at least this much of the slice left
LONG_WAIT_H = 24  # a wait on jobs longer than this wakes the model to look at them
REPORT_TURNS = 2  # writer turns for one report; then the goal ends without it (an incident says so)
WRITING = ("due", "writing", "requested")


def when(moment: datetime) -> str:
    """A time for the student: '2026-09-25 15:04 UTC'."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _hours_since(stamp_text: str) -> float:
    try:
        then = datetime.fromisoformat(stamp_text)
    except (TypeError, ValueError):
        return 0.0
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def did_work(outcome: Outcome) -> bool:
    """A usage limit, a failure, a time-out or a full context can arrive mid-turn, after real work (tool
    calls, model turns, cost): that turn counts. One that ended before any work (a lost login, a broken or
    hanging CLI) is not counted against the budget."""
    if outcome.status == "ok":
        return True
    return ((outcome.turns or 0) > 1 or bool(outcome.cost_usd) or bool(outcome.text)
            or bool((outcome.details or {}).get("tool_calls")))
ACTIVE = ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED")
SYSTEM = Actor(kind="system", client="sc-hub lab agent")


def report_since(goal: Goal, checkpoint: Checkpoint) -> str | None:
    """When the report is due from (this completion, or the owner's request), or None when none is due.
    Only a completion a slice saw (or `goal-report`) makes one due: goals completed earlier stay quiet."""
    if checkpoint.disposition != "complete":
        return None
    state = goal.report_state()
    if state.get("for") != checkpoint.updated or state.get("status") not in WRITING:
        return None
    since = str(state.get("requested_at") or checkpoint.updated)
    if ReportStore(goal.project_dir).published_since(since) is not None:
        return None  # already published (by an earlier writer turn, or from a chat)
    return since


class GoalBusy(GoalError):
    """Another slice of this goal runs: it arms its own successor, so this one just leaves."""


OWNER_BEAT_S = 30  # the holding slice touches owner.lock this often
OWNER_STALE_S = 180  # untouched this long: its holder is dead (node lost, killed), so the lock is broken


def _take(path: Path, wait_s: float):
    """The goal's lock, O_CREAT|O_EXCL with a heartbeat: flock would only exclude slices on the same node
    (Lustre is mounted localflock), and the gpu partition puts slices on any node."""
    lock = exclusive(path, wait_s=wait_s, stale_after_s=OWNER_STALE_S, heartbeat_s=OWNER_BEAT_S)
    lock.__enter__()
    return lock


@contextmanager
def held(goal: Goal, slurm: Slurm, job: str) -> Iterator[None]:
    """One slice holds a goal. Another slice of it RUNNING means this one leaves (GoalBusy). With none running,
    the holder has just ended or died: its lock stops being touched and is broken after OWNER_STALE_S."""
    goal.state.mkdir(parents=True, exist_ok=True)
    path = goal.state / "owner.lock"
    try:
        lock = _take(path, wait_s=1.0)
    except LockTimeout:
        running = [j.job_id for j in slurm.my_jobs() if j.name in goal.job_names and j.state == "RUNNING"
                   and j.job_id != job]
        if running:
            raise GoalBusy(f"another slice holds the goal of {goal.project} (running: {running})") from None
        try:
            lock = _take(path, wait_s=OWNER_STALE_S + 30)
        except LockTimeout:
            raise GoalBusy(f"the goal of {goal.project} stays locked with no slice running") from None
        goal.event("stale_lock_broken", job=job)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


class Slice:
    def __init__(self, settings: Settings, slurm: Slurm, project: str, job: str,
                 adapters: Mapping[str, Engine] = ADAPTERS, clock=time.monotonic) -> None:
        self.settings, self.slurm, self.project, self.job = settings, slurm, project, job
        self.clock, self.started = clock, clock()
        self.goal = Goal(settings, project)
        self.adapters = adapters
        self.cooldown = Cooldown(settings.bench_dir / "engine-cooldown.json")
        self.stuck: list[str] = []  # waited-for jobs Slurm will never start (said in the prompt)
        self.slow: list[str] = []  # waited-for jobs still pending after LONG_WAIT_H (said in the prompt)

    # ---- the slice ----------------------------------------------------------------------

    def run(self) -> str:
        goal = self.goal
        try:
            config = goal.config()
        except GoalError as exc:
            goal.event("invalid_goal", job=self.job, error=str(exc))
            return "invalid"
        if goal.stopped():
            goal.event("stop", job=self.job)
            self._withdraw_queued("the lab agent was stopped")
            return "stopped"
        checkpoint = CheckpointStore(goal.project_dir).read()
        if report_since(goal, checkpoint) is None and (checkpoint.disposition in TERMINAL or goal.report_only):
            goal.event("terminal", job=self.job, disposition=checkpoint.disposition)
            self._withdraw_queued(f"the lab agent's goal is {checkpoint.disposition or 'over'}")
            return "done"
        try:
            with self._lock():
                return self._locked(config, checkpoint)
        except GoalBusy as exc:
            goal.event("busy", job=self.job, detail=str(exc))
            return "busy"

    def _locked(self, config: GoalConfig, checkpoint) -> str:
        goal = self.goal
        if report_since(goal, checkpoint) is None and goal.turns() >= config.max_turns:
            return self._budget_spent(config)  # before arming: a successor would only find the goal over
        successor, created = self.arm_successor(config)
        self.successor = successor
        goal.event("successor", job=self.job, successor=successor, created=created)
        if not created:
            return "deferred"  # another slice of this goal is already queued: it does the work
        waiting = self._open_waiting_jobs(checkpoint)
        if waiting:
            goal.event("waiting", job=self.job, jobs=waiting)
            return "waiting"
        return self._turns(config)

    def _turns(self, config: GoalConfig) -> str:
        store = CheckpointStore(self.goal.project_dir)
        checkpoint = store.read()
        if report_since(self.goal, checkpoint) is not None:
            return self._engine_turns(config, "writer")
        if checkpoint.disposition in TERMINAL or self.goal.report_only:  # changed since run() looked
            self.goal.event("terminal", job=self.job, disposition=checkpoint.disposition)
            return "done"
        status = self._engine_turns(config, "research")
        checkpoint = store.read()
        if checkpoint.disposition in TERMINAL:  # its research cells still queued have no reader now
            self._withdraw_queued(f"the lab agent's goal is {checkpoint.disposition}")
        if config.report and checkpoint.disposition == "complete" and \
                self.goal.report_state().get("for") != checkpoint.updated:
            self.goal.save_report_state({"for": checkpoint.updated, "status": "due", "writer_turns": 0,
                                         "sessions": {}})
            self.goal.event("report_due", job=self.job)
            if report_since(self.goal, checkpoint) is not None and self.remaining_s(config) >= MIN_TURN_S:
                status = self._engine_turns(config, "writer")  # else the armed successor writes it
        return status

    def _engine_turns(self, config: GoalConfig, role: str) -> str:
        goal = self.goal
        if role == "research" and goal.turns() >= config.max_turns:
            return self._budget_spent(config)
        if role == "writer":
            self._start_writing()
        try:
            policy = load_policy(self.settings.bench_dir / "engine-policy.json")
            engines = order(policy, self.project, config.engine, lambda e: self._ready(e, policy))
        except PolicyError as exc:
            if role == "writer":  # the study stays complete; only its report cannot be written
                self._give_up_report(f"the engine policy allows no engine for it ({exc})")
                return "refused"
            CheckpointStore(goal.project_dir).write("blocked", reason=f"engine policy: {exc}", actor=SYSTEM)
            self._incident(f"The lab agent cannot run: {exc} It stops here; fix goal.md or ask the owner, then "
                           "start it again.")
            return "refused"
        if policy.weekly_turns and self._turns_this_week() >= policy.weekly_turns:
            goal.event("weekly_ceiling", job=self.job, ceiling=policy.weekly_turns)
            return "weekly_ceiling"
        if not engines:
            paused = {e: self._paused_until(e, policy, config) for e in policy.allowed(self.project)}
            goal.event("all_paused", job=self.job, until={k: str(v) for k, v in paused.items()})
            self._sleep_until(min((v for v in paused.values() if v is not None), default=None))
            return "paused"
        status = "usage_limited"
        for index, engine in enumerate(engines[:2]):  # mixed: when one engine hits its limit, the other goes on
            if index and self.remaining_s(config) < MIN_TURN_S:
                goal.event("no_time_for_other_engine", job=self.job, engine=engine)
                break
            outcome = self.turn(engine, config, policy, role=role)
            status = outcome.status
            if status in ("failed", "timed_out") and not did_work(outcome):
                continue  # nothing was done: the other engine may still answer
            if status != "usage_limited":
                break
            until = self.cooldown.mark(engine, outcome.error, resets=reset_hint(outcome.details))
            written = role == "writer" and self.goal.report_state().get("status") != "writing"
            other = index == 0 and len(engines) > 1 and not written
            if self.cooldown.streak(engine) == 1:  # one note per episode, not one per retry
                self._incident(f"{engine} hit its usage limit; it pauses until {when(until)}"
                               + (" and the other engine takes over." if other else "."))
            if written:
                break  # published (or given up) before the limit: nothing is left for the other engine
        return status

    # ---- one turn -----------------------------------------------------------------------

    def turn(self, engine: str, config: GoalConfig, policy, rotated: bool = False, role: str = "research") -> Outcome:
        goal = self.goal
        writing = role == "writer"
        sessions = self._sessions(role)
        saved = sessions.get(engine) or {}
        resume = saved.get("session_id")
        handover = not writing and resume is None and (goal.turns() > 0 or rotated)
        missed = not writing and resume is not None and sessions.get("last") not in (None, engine)
        new_id = str(uuid.uuid4()) if resume is None and engine == "claude" else None
        run_dir = goal.state / "runs" / self.job / (engine + ("-writer" if writing else "") + ("-rotated" if rotated else ""))
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = self.writer_prompt(config) if writing else self.prompt(config, handover, missed)
        (run_dir / "prompt.md").write_text(prompt)
        goal.event("turn_started", job=self.job, engine=engine, turn=goal.turns() + 1, resume=resume,
                   handover=handover, role=role, login=credential_fingerprint(engine))
        turn = Turn(prompt=prompt, cwd=run_dir, run_dir=run_dir, timeout_s=max(60, self.remaining_s(config)),
                    session_id=resume, new_session_id=new_id, model=policy.model(engine), effort=policy.effort(engine),
                    mcp=self.mcp_server(engine, policy.model(engine), policy.effort(engine), resume or new_id or "",
                                        role=role))
        guards = install_guards(self.settings.bench_dir)
        outcome = self.adapters[engine].run(turn, binary=str(guards / engine), env=self.engine_env(guards))
        if engine == "claude":
            window.record(self.settings.bench_dir, outcome.details.get("rate_limit"))
        write_json_atomic(run_dir / "outcome.json", dataclasses.asdict(outcome))
        worked = did_work(outcome)
        goal.event("turn_finished", job=self.job, engine=engine, status=outcome.status, role=role,
                   session=outcome.session_id, cost_usd=outcome.cost_usd, turns=outcome.turns, worked=worked)
        missing = outcome.status == "session_missing"
        if worked:  # a turn refused (a usage limit, a failure) before doing anything is neither counted nor kept
            if not writing:
                goal.count_turn(engine, self.job)
            self._log_usage(engine)
            self.cooldown.answered(engine)
        if missing and not rotated:  # lost, or too full to go on (counted above if it worked first): a new one
            self._save_sessions(role, {**sessions, engine: None})
            if not writing and goal.turns() >= config.max_turns:
                return outcome  # that turn spent the budget: the next slice ends the goal
            if self.remaining_s(config) < MIN_TURN_S:
                return outcome  # too little of the slice left: the next slice starts the new session
            return self.turn(engine, config, policy, rotated=True, role=role)
        if outcome.session_id and worked and not missing:
            last = {} if writing else {"last": engine}
            self._save_sessions(role, {**sessions, engine: {"session_id": outcome.session_id,
                                                            "since": saved.get("since") or self.job}, **last})
        if writing and worked:
            self._after_writing()
        if outcome.status == "failed" and worked:
            self._incident(f"The lab agent's {engine} turn failed: {outcome.error[-400:]}")
        elif outcome.status in ("failed", "timed_out") and not worked:
            self._engine_failing(engine, outcome)
        elif missing and rotated:
            self._incident(f"{engine} could not continue the work, not even in a fresh conversation: "
                           f"{outcome.error[-300:]}. The next turn tries again.")
        return outcome

    def _engine_failing(self, engine: str, outcome: Outcome) -> None:
        """A turn that failed before any work: paused longer each time in a row, one incident per pause."""
        count, until = self.cooldown.failing(engine, outcome.error or outcome.status,
                                             login=sign_in_fingerprint(engine))
        self.goal.event("engine_failing", job=self.job, engine=engine, in_a_row=count,
                        paused_until=until.isoformat(timespec="minutes") if until else None)
        if until is None:
            return
        why = outcome.error[-300:].strip() if outcome.error else "no answer before the turn's time limit"
        pace = self.goal.config().pace_minutes
        self._incident(
            f"{LABELS[engine]} on the cluster failed {count} times in a row before doing any work ({why}). No turn "
            f"is counted; the lab agent tries it again at {when(until)}. If its sign-in expired, sign in again: "
            f"{SIGN_IN_HOW}. The lab agent notices a new sign-in within {pace} minutes.")

    def _ready(self, engine: str, policy) -> bool:
        """Not paused after a usage limit, and (Claude) not above the owner's weekly ceiling."""
        if self.cooldown.relogged(engine, sign_in_fingerprint(engine)):
            self.goal.event("signed_in_again", job=self.job, engine=engine)  # its failing pause ends
        if not self.cooldown.ready(engine):
            return False
        if engine == "claude":
            full = window.above_ceiling(self.settings.bench_dir, policy.claude_weekly_ceiling)
            if full is not None:
                self.goal.event("claude_above_weekly_ceiling", job=self.job, utilization=full[0],
                                ceiling=policy.claude_weekly_ceiling, resets=full[1].isoformat(timespec="minutes"))
                return False
        return True

    def _sessions(self, role: str) -> dict:
        return self.goal.report_state().get("sessions") or {} if role == "writer" else self.goal.sessions()

    def _save_sessions(self, role: str, sessions: dict) -> None:
        if role == "writer":
            self.goal.save_report_state({**self.goal.report_state(), "sessions": sessions})
        else:
            self.goal.save_sessions(sessions)

    def _start_writing(self) -> None:
        state = self.goal.report_state()
        if state.get("status") != "writing":
            self.goal.save_report_state({**state, "status": "writing"})

    def _after_writing(self) -> None:
        """Published, still writing, or given up after REPORT_TURNS turns (the goal ends either way)."""
        goal = self.goal
        checkpoint = CheckpointStore(goal.project_dir).read()
        state = goal.report_state()
        turns = int(state.get("writer_turns", 0)) + 1
        since = str(state.get("requested_at") or checkpoint.updated)
        published = ReportStore(goal.project_dir).published_since(since)
        goal.save_report_state({**state, "writer_turns": turns})
        if published is not None:
            goal.save_report_state({**goal.report_state(), "status": "published", "folder": published.folder})
            goal.event("report_published", job=self.job, folder=published.folder)
        elif turns >= REPORT_TURNS:
            self._give_up_report(f"it was not published in {REPORT_TURNS} turns")

    def _give_up_report(self, reason: str) -> None:
        self.goal.save_report_state({**self.goal.report_state(), "status": "gave_up", "reason": reason})
        self._incident(f"The lab agent could not write the report: {reason}. The research itself is complete. Ask "
                       f"your assistant to write it with the report tool, or run `schub goal-report {self.project}`.")

    def _paused_until(self, engine: str, policy, config: GoalConfig) -> datetime | None:
        until = self.cooldown.until(engine)
        if until is not None and self.cooldown.kind(engine) == "failing":  # a new sign-in ends it: look each pace
            until = min(until, datetime.now(timezone.utc) + timedelta(minutes=config.pace_minutes))
        full = window.above_ceiling(self.settings.bench_dir, policy.claude_weekly_ceiling) if engine == "claude" else None
        return max((d for d in (until, full[1] if full else None) if d is not None), default=None)

    def _sleep_until(self, when: datetime | None) -> None:
        """Every engine is paused: the armed successor starts at the earliest reset, not every few minutes
        on the owner's gpu budget."""
        successor = getattr(self, "successor", None)
        if when is None or not successor:
            return
        minutes = int((when - datetime.now(timezone.utc)).total_seconds() // 60) + 1
        if minutes <= 1:
            return
        try:
            self.slurm.delay(successor, minutes)
            self.goal.event("successor_delayed", job=self.job, successor=successor, minutes=minutes)
        except SlurmError as exc:
            self.goal.event("successor_delay_failed", job=self.job, error=str(exc)[:300])

    def remaining_s(self, config: GoalConfig) -> int:
        """What is left of this slice's time for a turn (one deadline per slice, not per engine)."""
        return int(config.slice_minutes * 60 - (self.clock() - self.started))

    def prompt(self, config: GoalConfig, handover: bool, missed: bool = False) -> str:
        p = self.project
        text = [
            "OBJECTIVE (the student's standing goal, unchanged):", config.objective, "",
            f"You are the lab agent of project \"{p}\". Work only through the sc-hub MCP tools. This turn runs in "
            f"Slurm job {self.job} for about {config.slice_minutes} minutes; the next turn continues later.",
            f"Start with journal(\"{p}\"): read the hand-over and the checkpoint, then do the next useful "
            "scientific step. Keep cells short, with why and expect; heavy work goes to %%slurm cells.",
            "Record reasoning with note(). Before you stop, call handoff() with where the work stands: "
            "disposition \"waiting\" with waiting_jobs when you wait for Slurm jobs (the next turn then starts "
            "only after they end), \"complete\" when the objective is reached, \"blocked\" when you need the "
            "student, else \"active\" with the next action.",
            "Never start another lab agent and never change the engine policy.",
        ]
        if handover:
            text += ["", "This is a new session: earlier turns (possibly by another engine) are not in this "
                     "conversation. Their state is in the journal's hand-over, checkpoint and entries; read them "
                     "before acting."]
        if missed:
            text += ["", "Since your last turn another engine worked on this goal: read the journal's new entries "
                     "and the hand-over before acting; your memory of the work is out of date."]
        if self.stuck:
            text += ["", "The job(s) you wait for may never start as they are: " + ", ".join(self.stuck)
                     + ". cluster() shows why each job waits. Cancel with stop(<job id>) and resubmit it fixed "
                     "(e.g. a shorter --time), or hand over as \"blocked\" when the student must act. A hold "
                     "placed by the cluster's admins (JobHeldAdmin) is theirs: hand over as \"blocked\", do not "
                     "work around it."]
        if self.slow:
            text += ["", f"The job(s) you wait for have been pending for over {LONG_WAIT_H} hours: "
                     + ", ".join(self.slow) + ". Look at them with cluster(). If they only queue behind other "
                     "work, hand over as \"waiting\" again; if they can never start as they are, cancel with "
                     "stop(<job id>) and resubmit them fixed."]
        return "\n".join(text)

    def writer_prompt(self, config: GoalConfig) -> str:
        p = self.project
        return "\n".join([
            "OBJECTIVE of the study (the student's goal, unchanged):", config.objective, "",
            f"The research of project \"{p}\" is complete. This turn is not research: write its report, the "
            "notebook a student or a professor reads to follow the whole study, in the language of the objective. "
            f"Work only through the sc-hub MCP tools; this turn runs in Slurm job {self.job} for about "
            f"{config.slice_minutes} minutes.",
            f"Read skills(\"report_writing\") first, then journal(\"{p}\"): the hand-over, the findings, verdicts, "
            "decisions and mistakes, and the cells they rest on. Build a draft with "
            f"report(\"{p}\", spec), read its warnings (advice, not failures), improve what matters, then call "
            f"report(\"{p}\", spec, publish=true).",
            "Do not start new analyses. When no cell shows something the reader needs (a summary table or figure "
            "from saved results), one short presentation cell is fine; say so in its why. Do not call handoff(): "
            "the research stays complete.",
            "Never start another lab agent and never change the engine policy.",
        ])

    def mcp_server(self, engine: str, model: str, effort: str, session: str, role: str = "research") -> McpServer:
        env = {k: os.environ[k] for k in (*PASS_THROUGH, "HOME", "USER", "PATH", "LANG") if os.environ.get(k)}
        env.update({k: v for k, v in os.environ.items() if k.startswith("SCHUB_BENCH_")})
        env.update(SCHUB_ROOT=str(self.settings.root), SCHUB_LAB_AGENT_ENGINE=engine, SCHUB_LAB_AGENT_MODEL=model,
                   SCHUB_LAB_AGENT_EFFORT=effort, SCHUB_LAB_AGENT_SESSION=session, SCHUB_GOAL_PROJECT=self.project)
        if role == "writer":
            env.update(SCHUB_LAB_AGENT_ROLE="writer")
        return McpServer(command=str(self.settings.python), args=("-m", "schub.cli", "mcp"),
                         env=tuple(sorted(env.items())))

    def engine_env(self, guards: Path) -> dict[str, str]:
        path = os.pathsep.join((str(guards), os.environ.get("PATH", "")))
        return {**os.environ, "PATH": path, "SCHUB_GOAL_PROJECT": self.project}

    # ---- successor, lock, waiting ---------------------------------------------------------

    def arm_successor(self, config: GoalConfig) -> tuple[str, bool]:
        """(job id, created): the next slice, armed before this one does anything else."""
        intent_path = self.goal.state / "intent.json"
        prior = read_json(intent_path) or {}
        if prior.get("comment") and not prior.get("job_id"):
            found = self.slurm.find_by_comment(prior["comment"])  # raises if the queue cannot be read
            if found:
                write_json_atomic(intent_path, {**prior, "job_id": found})
            else:
                write_json_atomic(intent_path, {**prior, "job_id": "never-submitted"})
        others = [j for j in self.slurm.my_jobs() if j.name in self.goal.job_names and j.job_id != self.job
                  and j.state == "PENDING"]  # a running one without the lock is leaving
        if others:
            return others[0].job_id, False
        return self.submit(config, after=self.job), True

    def submit(self, config: GoalConfig, after: str | None) -> str:
        comment = f"schub-goal-{secrets.token_hex(6)}"
        write_json_atomic(self.goal.state / "intent.json", {"comment": comment, "after": after, "job_id": None})
        begin = f"now+{config.pace_minutes}minutes" if after else "now+60"  # seconds
        bench, error = self.settings.bench, None
        for partition in dict.fromkeys((bench.background_partition, bench.background_fallback)):
            spec = self._spec(config, partition, comment, begin, after)
            script = self.goal.state / "scripts" / f"slice-{secrets.token_hex(4)}.sbatch"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(render_script(spec))
            try:
                job_id = self.slurm.submit(script)
            except SlurmError as exc:
                error, found = exc, self.slurm.find_by_comment(comment)  # did it go through after all?
                if not found:
                    continue
                job_id = found
            write_json_atomic(self.goal.state / "intent.json", {"comment": comment, "after": after, "job_id": job_id,
                                                               "partition": partition})
            return job_id
        raise GoalError(f"could not queue the next slice: {error}")

    def _spec(self, config: GoalConfig, partition: str, comment: str, begin: str, after: str | None) -> JobSpec:
        s = self.settings
        env = [(k, os.environ[k]) for k in PASS_THROUGH if os.environ.get(k)]
        env += [(k, v) for k, v in sorted(os.environ.items()) if k.startswith("SCHUB_BENCH_")]
        env += [("SCHUB_ROOT", str(s.root)), ("SCHUB_PYTHON", str(s.python)), ("PYTHONUNBUFFERED", "1")]
        if s.library:
            env.append(("SCHUB_LIBRARY", str(s.library)))
        (self.goal.state / "logs").mkdir(parents=True, exist_ok=True)  # Slurm does not create --output folders
        return JobSpec(name=self.goal.job_name, partition=partition,
                       resources=Resources(cpus=1, mem_gb=4, time_min=config.slice_minutes + 10),
                       log_path=self.goal.state / "logs" / "slice-%j.log", workdir=self.goal.folder,
                       command=(str(s.python), "-m", "schub.bench.goal_agent", "--project", self.project, "--run"),
                       env=tuple(dict(env).items()), comment=comment, begin=begin, signal="B:TERM@300",
                       after_any=(after,) if after else ())

    def _lock(self):
        return held(self.goal, self.slurm, self.job)

    def _open_waiting_jobs(self, checkpoint) -> list[str]:
        """The checkpoint's waiting jobs still queued or running; any finished one means: call the model."""
        if checkpoint.disposition != "waiting" or not checkpoint.waiting_jobs:
            return []
        ids = [j.job_id for j in checkpoint.waiting_jobs]
        try:
            states = self.slurm.states(ids)
        except SlurmError:
            return ids  # Slurm did not answer: keep waiting rather than wake the model for nothing
        if not all(states.get(i, "").split(" ")[0] in ACTIVE for i in ids):
            return []
        try:
            stuck = [f"{j.job_id} ({j.reason.strip('()')})" for j in self.slurm.my_jobs()
                     if j.job_id in ids and j.state == "PENDING" and STUCK.search(j.reason)]
        except SlurmError:
            return ids
        if stuck:  # waiting on these would last forever: the model decides (cancel and resubmit, or hand over)
            self.stuck = stuck
            self.goal.event("stuck_jobs", job=self.job, jobs=stuck)
            return []
        pending = [i for i in ids if states.get(i, "").split(" ")[0] == "PENDING"]
        if pending and _hours_since(checkpoint.updated) > LONG_WAIT_H:  # e.g. behind a parent that is stuck
            self.slow = pending
            self.goal.event("long_wait", job=self.job, jobs=pending)
            return []
        return ids

    # ---- records ----------------------------------------------------------------------

    def _budget_spent(self, config: GoalConfig) -> str:
        CheckpointStore(self.goal.project_dir).write("blocked", reason=f"the goal's budget of {config.max_turns} "
                                                     "turns is spent", actor=SYSTEM)
        self._incident(f"The lab agent stopped: its budget of {config.max_turns} turns is spent. Raise "
                       "max_turns in goal/goal.md and start it again to continue.")
        self._withdraw_queued("the lab agent's budget of turns is spent")
        return "budget"

    def _withdraw_queued(self, why: str) -> None:
        """Research cells the lab agent queued that no workbench took yet: withdrawn, so no node runs them
        for nobody. The student's own cells and the report writer's stay."""
        def mine(request) -> bool:
            return request.actor.kind == "lab_agent" and request.actor.role != "writer"

        try:
            gone = Inbox(self.settings.bench_dir).withdraw(
                self.project, f"withdrawn before it started: {why}", mine)
        except OSError as exc:
            self.goal.event("withdraw_failed", job=self.job, error=str(exc)[:300])
            return
        if gone:
            self.goal.event("withdrawn", job=self.job, cells=gone, why=why)

    def _incident(self, text: str) -> None:
        Journal(self.goal.project_dir, self.project).add_note("incident", text, audience="both", actor=SYSTEM)
        self.goal.event("incident", job=self.job, text=text)

    def _log_usage(self, engine: str) -> None:
        self.settings.bench_dir.mkdir(parents=True, exist_ok=True)
        record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "engine": engine,
                  "project": self.project, "job": self.job}
        with (self.settings.bench_dir / "engine-usage.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def _turns_this_week(self) -> int:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        try:
            lines = (self.settings.bench_dir / "engine-usage.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return 0
        count = 0
        for line in lines:
            try:
                count += datetime.fromisoformat(json.loads(line)["at"]) >= since
            except (ValueError, KeyError, TypeError):
                continue  # a torn or foreign line
        return count


def start(settings: Settings, slurm: Slurm, project: str, text: str) -> str:
    """Write the goal and queue its first slice (the student's `schub goal-start`)."""
    goal = Goal(settings, project)
    if not goal.project_dir.is_dir():
        raise GoalError(f"project {project!r} does not exist")
    config = parse_goal(text)
    order(load_policy(settings.bench_dir / "engine-policy.json"), project, config.engine, lambda e: True)
    try:
        previous = goal.config().objective
    except GoalError:
        previous = None
    if previous is not None and previous != config.objective:
        goal.archive_state()
    goal.write(text)
    (goal.folder / "STOP").unlink(missing_ok=True)
    goal.report_only_marker.unlink(missing_ok=True)  # a real goal now: its research turns may run
    store = CheckpointStore(goal.project_dir)
    if store.read().disposition in TERMINAL:  # a restart: the goal is open again
        store.write("active", next_action="continue the goal from the journal's hand-over", actor=SYSTEM)
    slice_ = Slice(settings, slurm, project, job="launch")
    policy = load_policy(settings.bench_dir / "engine-policy.json")
    for engine in policy.allowed(project):  # started again (e.g. after signing in again): try failing engines now
        slice_.cooldown.clear(engine, only_failing=True)
    _keep_watched(settings, slurm, goal)
    active = [j for j in slurm.my_jobs() if j.name in goal.job_names and j.state in ACTIVE]
    if active:
        _bring_forward(slurm, goal, active)
        goal.event("start_skipped", queued=[j.job_id for j in active])
        return active[0].job_id
    job = slice_.submit(config, after=None)
    goal.event("started", job=job, engine=config.engine, max_turns=config.max_turns)
    return job


def _keep_watched(settings: Settings, slurm: Slurm, goal: Goal) -> None:
    """A lapsed watchdog (a quiet bench) starts again: it re-arms broken chains and checks the engines."""
    try:
        Workbench(settings, slurm).ensure_watchdog()
    except (SlurmError, OSError) as exc:
        goal.event("watchdog_not_started", error=str(exc)[:300])


def _bring_forward(slurm: Slurm, goal: Goal, jobs) -> None:
    """A slice already queued but put off (paused engines, the pace) starts now: the student asked."""
    for job in jobs:
        if job.state == "PENDING" and "BeginTime" in job.reason:
            try:
                slurm.delay(job.job_id, 1)
                goal.event("brought_forward", job=job.job_id)
            except SlurmError as exc:
                goal.event("bring_forward_failed", job=job.job_id, error=str(exc)[:300])


def active_goals(settings: Settings) -> list[str]:
    """Projects whose lab agent should be working: a goal, no STOP, not complete or blocked (or a report due)."""
    found = []
    patterns = ("*/goal/goal.md", "*/*/goal/goal.md", "*/*/*/goal/goal.md")  # projects and subprojects
    files = [f for pattern in patterns for f in settings.projects_dir.glob(pattern)] if settings.projects_dir.is_dir() else []
    for folder in sorted(files):
        goal = Goal(settings, folder.parent.parent.relative_to(settings.projects_dir).as_posix())
        checkpoint = CheckpointStore(goal.project_dir).read()
        working = checkpoint.disposition not in TERMINAL and not goal.report_only
        if not goal.stopped() and (working or report_since(goal, checkpoint)):
            found.append(goal.project)
    return found


def revive(settings: Settings, slurm: Slurm) -> list[str]:
    """The watchdog's repair: an active goal with no slice queued or running gets one (a chain broken by a
    dead node or a failed sbatch)."""
    queued = {j.name for j in slurm.my_jobs() if j.state in ACTIVE}
    revived = []
    for project in active_goals(settings):
        goal = Goal(settings, project)
        if queued & set(goal.job_names):
            continue
        try:
            job = Slice(settings, slurm, project, job="watchdog").submit(goal.config(), after=None)
        except (GoalError, SlurmError) as exc:
            goal.event("revive_failed", error=str(exc)[:500])
            continue
        goal.event("revived_by_watchdog", job=job)
        revived.append(f"{project} (job {job})")
    return revived


def stop(settings: Settings, project: str) -> str:
    goal = Goal(settings, project)
    goal.folder.mkdir(parents=True, exist_ok=True)
    (goal.folder / "STOP").write_text("")
    goal.event("stop_requested")
    return f"goal/STOP written: the next slice of {project} ends the chain (queued slices exit at once)"


def request_report(settings: Settings, slurm: Slurm, project: str) -> str:
    """The owner's `schub goal-report`: a writer turn for a project already handed over as complete (by a lab
    agent or a chat). A project without a goal gets a minimal one, with no research budget."""
    goal = Goal(settings, project)
    if not goal.project_dir.is_dir():
        raise GoalError(f"project {project!r} does not exist")
    checkpoint = CheckpointStore(goal.project_dir).read()
    if checkpoint.disposition != "complete":
        raise GoalError(f"{project} is {checkpoint.disposition}, not complete: the report is written once the work "
                        "is handed over as complete")
    if not (goal.folder / "goal.md").exists():
        try:
            question = ProjectStore(settings).meta(project).question
        except ProjectError:
            question = ""
        goal.write(f"---\nmax_turns: 1\nreport: no\n---\nThe research was done in chat; only its report is "
                   f"written here. The question: {question or project}\n")
        goal.report_only_marker.write_text("made by schub goal-report: no research turns, only reports\n")
    order(load_policy(settings.bench_dir / "engine-policy.json"), project, goal.config().engine, lambda e: True)
    goal.save_report_state({"for": checkpoint.updated, "status": "requested", "requested_at": stamp(),
                            "writer_turns": 0, "sessions": {}})
    (goal.folder / "STOP").unlink(missing_ok=True)
    _keep_watched(settings, slurm, goal)
    active = [j for j in slurm.my_jobs() if j.name in goal.job_names and j.state in ACTIVE]
    if active:
        _bring_forward(slurm, goal, active)
        goal.event("report_requested", queued=[j.job_id for j in active])
        return active[0].job_id
    job = Slice(settings, slurm, project, job="report").submit(goal.config(), after=None)
    goal.event("report_requested", job=job)
    return job


def status(settings: Settings, slurm: Slurm, project: str) -> dict:
    goal = Goal(settings, project)
    try:
        config = dataclasses.asdict(goal.config())
        config["objective"] = config["objective"][:300]
    except GoalError as exc:
        config = {"error": str(exc)}
    try:
        queued = [j.model_dump() for j in slurm.my_jobs() if j.name in goal.job_names]
    except SlurmError as exc:
        queued = [{"error": str(exc)}]
    return {"project": project, "goal": config, "stopped": goal.stopped(), "turns": goal.turns(),
            "sessions": goal.sessions(), "slices": queued, "checkpoint": CheckpointStore(goal.project_dir).read()
            .model_dump(mode="json"), "report": {k: v for k, v in goal.report_state().items() if k != "sessions"},
            "events": goal.events(10)}


def _ended(_number: int, _frame: object) -> None:
    raise KeyboardInterrupt  # Slurm's time limit or scancel: record it and leave


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schub.bench.goal_agent")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run", action="store_true", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    job = os.environ.get("SLURM_JOB_ID", "")
    if not job:
        sys.stderr.write("goal_agent --run belongs inside a Slurm job\n")
        return 2
    signal.signal(signal.SIGTERM, _ended)
    settings = load_settings()
    try:
        result = Slice(settings, Slurm(), args.project, job).run()
    except KeyboardInterrupt:
        Goal(settings, args.project).event("slice_ended_by_slurm", job=job)
        return 1
    except Exception as exc:  # noqa: BLE001 - recorded where the student sees it, then the slice ends
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        goal = Goal(settings, args.project)
        goal.event("slice_error", job=job, error=detail)
        try:
            Journal(goal.project_dir, args.project).add_note("incident", f"A lab-agent slice failed: {detail}",
                                                             actor=SYSTEM)
        except Exception:  # noqa: BLE001 - the event above already holds it
            pass
        traceback.print_exc()
        return 1
    print(f"slice {job} of {args.project}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
