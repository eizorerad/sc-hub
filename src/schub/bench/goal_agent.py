"""The lab agent: a standing goal worked on in Slurm slices by Claude Code or Codex
(the VCC2026 goal-agent pattern, through the bench's own MCP tools and journal).

    python -m schub.bench.goal_agent --project P --run     # inside a slice job

Each slice, in order: the STOP files and a finished checkpoint end the chain; one
slice holds the goal (flock); it arms its successor FIRST (afterany:self,
--begin=now+pace), with the intent written before sbatch so an ambiguous failure is
recovered by comment, never resubmitted blind; while the checkpoint waits on Slurm
jobs that are still queued or running it ends without calling a model; then the
turn budget, the owner's weekly ceiling, the engine policy and the pauses pick an
engine, and one turn runs, resuming that engine's session. A new session (another
engine, or a lost one) starts from the journal's hand-over, not someone else's chat.
The engine runs through bench/guard/<engine> with only the sc-hub MCP server, so its
cells land in the journal like a student's chat agent's, marked lab_agent.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import secrets
import signal
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Mapping

from ..bricks import Resources
from ..config import Settings, load_settings
from ..slurm import JobSpec, Slurm, SlurmError, render_script
from .checkpoint import CheckpointStore
from .engines.base import GUARD_DIR, Engine, McpServer, Outcome, Turn, credential_fingerprint
from .engines.claude import Claude
from .engines.codex import Codex
from .engines.cooldown import Cooldown
from .engines.policy import PolicyError, load as load_policy, order
from .fsio import read_json, write_json_atomic
from .goal import Goal, GoalConfig, GoalError, parse_goal
from .journal import Journal
from .models import Actor
from .workbench import PASS_THROUGH

ADAPTERS: dict[str, Engine] = {"claude": Claude(), "codex": Codex()}
TERMINAL = ("complete", "blocked")
WORKED = ("ok", "timed_out", "failed")  # outcomes of a turn in which the engine did (or may have done) work
ACTIVE = ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED")
SYSTEM = Actor(kind="system", client="sc-hub lab agent")


@contextmanager
def held(goal: Goal, slurm: Slurm, job: str) -> Iterator[None]:
    """One slice holds a goal. A lock whose holder cannot be alive (no other slice of this goal is
    RUNNING: its node died, and the file server kept the lock) is set aside once."""
    goal.state.mkdir(parents=True, exist_ok=True)
    path = goal.state / "owner.lock"
    for attempt in (1, 2):
        handle = path.open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            handle.close()
            running = [j.job_id for j in slurm.my_jobs() if j.name == goal.job_name and j.state == "RUNNING"
                       and j.job_id != job]
            if attempt == 2 or running:
                raise GoalError(f"another slice holds the goal of {goal.project} (running: {running})")
            path.rename(path.with_name(f"owner.lock.stale-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"))
            goal.event("stale_lock_set_aside", job=job)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class Slice:
    def __init__(self, settings: Settings, slurm: Slurm, project: str, job: str,
                 adapters: Mapping[str, Engine] = ADAPTERS) -> None:
        self.settings, self.slurm, self.project, self.job = settings, slurm, project, job
        self.goal = Goal(settings, project)
        self.adapters = adapters
        self.cooldown = Cooldown(settings.bench_dir / "engine-cooldown.json")

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
            return "stopped"
        checkpoint = CheckpointStore(goal.project_dir).read()
        if checkpoint.disposition in TERMINAL:
            goal.event("terminal", job=self.job, disposition=checkpoint.disposition)
            return "done"
        with self._lock():
            successor, created = self.arm_successor(config)
            goal.event("successor", job=self.job, successor=successor, created=created)
            if not created:
                return "deferred"  # another slice of this goal is already queued: it does the work
            waiting = self._open_waiting_jobs(checkpoint)
            if waiting:
                goal.event("waiting", job=self.job, jobs=waiting)
                return "waiting"
            return self._turns(config)

    def _turns(self, config: GoalConfig) -> str:
        goal = self.goal
        if goal.turns() >= config.max_turns:
            CheckpointStore(goal.project_dir).write("blocked", reason=f"the goal's budget of {config.max_turns} "
                                                    "turns is spent", actor=SYSTEM)
            self._incident(f"The lab agent stopped: its budget of {config.max_turns} turns is spent. Raise "
                           "max_turns in goal/goal.md and start it again to continue.")
            return "budget"
        try:
            policy = load_policy(self.settings.bench_dir / "engine-policy.json")
            engines = order(policy, self.project, config.engine, self.cooldown.ready)
        except PolicyError as exc:
            self._incident(f"The lab agent cannot run: {exc}")
            return "refused"
        if policy.weekly_turns and self._turns_this_week() >= policy.weekly_turns:
            goal.event("weekly_ceiling", job=self.job, ceiling=policy.weekly_turns)
            return "weekly_ceiling"
        if not engines:
            paused = {e: self.cooldown.until(e) for e in policy.allowed(self.project)}
            goal.event("all_paused", job=self.job, until={k: str(v) for k, v in paused.items()})
            return "paused"
        status = "usage_limited"
        for engine in engines[:2]:  # mixed: when the first engine hits its limit, the other takes the turn
            outcome = self.turn(engine, config, policy)
            status = outcome.status
            if status != "usage_limited":
                break
            until = self.cooldown.mark(engine, outcome.error)
            self._incident(f"{engine} hit its usage limit; it pauses until {until.isoformat(timespec='minutes')}"
                           + (" and the other engine takes over." if len(engines) > 1 else "."))
        return status

    # ---- one turn -----------------------------------------------------------------------

    def turn(self, engine: str, config: GoalConfig, policy, rotated: bool = False) -> Outcome:
        goal = self.goal
        sessions = goal.sessions()
        saved = sessions.get(engine) or {}
        resume = saved.get("session_id")
        handover = resume is None and (goal.turns() > 0 or rotated)
        missed = resume is not None and sessions.get("last") not in (None, engine)
        new_id = str(uuid.uuid4()) if resume is None and engine == "claude" else None
        run_dir = goal.state / "runs" / self.job / (engine + ("-rotated" if rotated else ""))
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = self.prompt(config, handover, missed)
        (run_dir / "prompt.md").write_text(prompt)
        goal.event("turn_started", job=self.job, engine=engine, turn=goal.turns() + 1, resume=resume,
                   handover=handover, login=credential_fingerprint(engine))
        turn = Turn(prompt=prompt, cwd=run_dir, run_dir=run_dir, timeout_s=config.slice_minutes * 60,
                    session_id=resume, new_session_id=new_id, model=policy.model(engine), effort=policy.effort(engine),
                    mcp=self.mcp_server(engine, policy.model(engine), policy.effort(engine), resume or new_id or ""))
        outcome = self.adapters[engine].run(turn, binary=str(GUARD_DIR / engine), env=self.engine_env())
        write_json_atomic(run_dir / "outcome.json", dataclasses.asdict(outcome))
        goal.event("turn_finished", job=self.job, engine=engine, status=outcome.status,
                   session=outcome.session_id, cost_usd=outcome.cost_usd, turns=outcome.turns)
        if outcome.status == "session_missing" and not rotated:
            goal.save_sessions({**sessions, engine: None})
            return self.turn(engine, config, policy, rotated=True)
        if outcome.status in WORKED:  # a turn refused for a usage limit did nothing: not counted, not kept
            goal.count_turn(engine, self.job)
            self._log_usage(engine)
        if outcome.session_id and outcome.status in WORKED:
            goal.save_sessions({**sessions, engine: {"session_id": outcome.session_id,
                                                     "since": saved.get("since") or self.job}, "last": engine})
        if outcome.status == "failed":
            self._incident(f"The lab agent's {engine} turn failed: {outcome.error[-400:]}")
        return outcome

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
        return "\n".join(text)

    def mcp_server(self, engine: str, model: str, effort: str, session: str) -> McpServer:
        env = {k: os.environ[k] for k in (*PASS_THROUGH, "HOME", "PATH", "LANG") if os.environ.get(k)}
        env.update({k: v for k, v in os.environ.items() if k.startswith("SCHUB_BENCH_")})
        env.update(SCHUB_ROOT=str(self.settings.root), SCHUB_LAB_AGENT_ENGINE=engine, SCHUB_LAB_AGENT_MODEL=model,
                   SCHUB_LAB_AGENT_EFFORT=effort, SCHUB_LAB_AGENT_SESSION=session, SCHUB_GOAL_PROJECT=self.project)
        return McpServer(command=str(self.settings.python), args=("-m", "schub.cli", "mcp"),
                         env=tuple(sorted(env.items())))

    def engine_env(self) -> dict[str, str]:
        path = os.pathsep.join((str(GUARD_DIR), os.environ.get("PATH", "")))
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
        others = [j for j in self.slurm.my_jobs() if j.name == self.goal.job_name and j.job_id != self.job
                  and j.state in ACTIVE]
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
        states = self.slurm.states(ids)
        if all(states.get(i, "").split(" ")[0] in ACTIVE for i in ids):
            return ids
        return []

    # ---- records ----------------------------------------------------------------------

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
        return sum(1 for line in lines if line.strip() and datetime.fromisoformat(json.loads(line)["at"]) >= since)


def start(settings: Settings, slurm: Slurm, project: str, text: str) -> str:
    """Write the goal and queue its first slice (the student's `schub goal-start`)."""
    goal = Goal(settings, project)
    if not goal.project_dir.is_dir():
        raise GoalError(f"project {project!r} does not exist")
    config = parse_goal(text)
    order(load_policy(settings.bench_dir / "engine-policy.json"), project, config.engine, lambda e: True)
    goal.write(text)
    (goal.folder / "STOP").unlink(missing_ok=True)
    slice_ = Slice(settings, slurm, project, job="launch")
    active = [j.job_id for j in slurm.my_jobs() if j.name == goal.job_name and j.state in ACTIVE]
    if active:
        goal.event("start_skipped", queued=active)
        return active[0]
    job = slice_.submit(config, after=None)
    goal.event("started", job=job, engine=config.engine, max_turns=config.max_turns)
    return job


def active_goals(settings: Settings) -> list[str]:
    """Projects whose lab agent should be working: a goal, no STOP, not complete or blocked."""
    found = []
    for folder in sorted(settings.projects_dir.glob("*/goal/goal.md")) if settings.projects_dir.is_dir() else []:
        goal = Goal(settings, folder.parent.parent.name)
        if not goal.stopped() and CheckpointStore(goal.project_dir).read().disposition not in TERMINAL:
            found.append(goal.project)
    return found


def revive(settings: Settings, slurm: Slurm) -> list[str]:
    """The watchdog's repair: an active goal with no slice queued or running gets one (a chain broken by a
    dead node or a failed sbatch)."""
    queued = {j.name for j in slurm.my_jobs() if j.state in ACTIVE}
    revived = []
    for project in active_goals(settings):
        goal = Goal(settings, project)
        if goal.job_name in queued:
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


def status(settings: Settings, slurm: Slurm, project: str) -> dict:
    goal = Goal(settings, project)
    try:
        config = dataclasses.asdict(goal.config())
        config["objective"] = config["objective"][:300]
    except GoalError as exc:
        config = {"error": str(exc)}
    try:
        queued = [j.model_dump() for j in slurm.my_jobs() if j.name == goal.job_name]
    except SlurmError as exc:
        queued = [{"error": str(exc)}]
    return {"project": project, "goal": config, "stopped": goal.stopped(), "turns": goal.turns(),
            "sessions": goal.sessions(), "slices": queued, "checkpoint": CheckpointStore(goal.project_dir).read()
            .model_dump(mode="json"), "events": goal.events(10)}


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
    except (GoalError, SlurmError, OSError) as exc:
        Goal(settings, args.project).event("slice_error", job=job, error=str(exc)[:1000])
        raise
    print(f"slice {job} of {args.project}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
