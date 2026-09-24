"""The lab agent's slices on a fake cluster with fake Claude Code and Codex (through the real guards)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from schub.bench import goal_agent
from schub.bench.checkpoint import CheckpointStore
from schub.bench.clients import actor_for
from schub.bench.engines import policy as engine_policy
from schub.bench.engines.cooldown import Cooldown
from schub.bench.engines.policy import PolicyError
from schub.bench.goal import Goal, GoalError, parse_goal
from schub.bench.goal_agent import Slice, revive
from schub.bench.journal import Journal
from schub.bench.models import WaitingJob
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster
from tests.fake_engines import calls, install

GOAL = """---
engine: auto
max_turns: 5
slice_minutes: 30
pace_minutes: 60
---
Build the K562 perturbation table and QC it.
"""


@pytest.fixture
def lab(settings: Settings, tmp_path: Path, monkeypatch) -> Settings:
    ProjectStore(settings).create("p", question="Is the K562 screen usable?")
    fakes = install(tmp_path / "fakebin")
    for key, value in {"FAKE_LOG": str(tmp_path / "calls.jsonl"), "SCHUB_CLAUDE_BIN": str(fakes["claude"]),
                       "SCHUB_CODEX_BIN": str(fakes["codex"]), "SCHUB_ROOT": str(settings.root),
                       "SCHUB_PYTHON": sys.executable,
                       "PYTHONPATH": str(Path(__file__).parents[1] / "src")}.items():
        monkeypatch.setenv(key, value)
    return settings


def incidents(settings: Settings) -> list[str]:
    return [e.text for e in Journal(settings.projects_dir / "p", "p").entries(kinds=("incident",), limit=100)]


def engine_calls() -> list[dict]:
    return calls(Path(os.environ["FAKE_LOG"]))


def start_and_run(settings: Settings, cluster: FakeCluster) -> tuple[str, str]:
    first = goal_agent.start(settings, Slurm(cluster), "p", GOAL)
    cluster.jobs[first] = "RUNNING"
    return first, Slice(settings, Slurm(cluster), "p", first).run()


def successor_of(cluster: FakeCluster, job: str) -> str:
    return next(j for j in cluster.scripts if cluster.dependencies(j) == f"afterany:{job}")


def next_slice(settings: Settings, cluster: FakeCluster, finished: str) -> tuple[str, str]:
    cluster.jobs[finished] = "COMPLETED"
    job = successor_of(cluster, finished)
    cluster.jobs[job] = "RUNNING"
    return job, Slice(settings, Slurm(cluster), "p", job).run()


def test_goal_files_are_checked() -> None:
    assert parse_goal(GOAL).max_turns == 5 and parse_goal("just an objective").engine == "auto"
    for bad in ("---\nengine: gpt\n---\nx", "---\nmax_turns: 0\n---\nx", "---\nsneaky: 1\n---\nx",
                "---\nslice_minutes: many\n---\nx", "---\nengine: auto\n---\n"):
        with pytest.raises(GoalError):
            parse_goal(bad)


def test_a_slice_arms_its_successor_before_the_turn(lab: Settings, cluster: FakeCluster) -> None:
    first, result = start_and_run(lab, cluster)
    assert result == "ok"
    script = cluster.scripts[first]
    assert "#SBATCH --begin=now+60\n" in script and "--dependency" not in script
    successor = successor_of(cluster, first)
    assert "#SBATCH --begin=now+60minutes" in cluster.scripts[successor]
    assert "#SBATCH --partition=gpu" in cluster.scripts[successor] and "--gres" not in cluster.scripts[successor]
    events = [e["event"] for e in Goal(lab, "p").events()]
    assert events.index("successor") < events.index("turn_started") < events.index("turn_finished")
    [call] = engine_calls()
    argv = call["argv"]
    assert call["engine"] == "claude" and "--session-id" in argv and "--resume" not in argv
    assert "Build the K562 perturbation table" in argv[argv.index("-p") + 1]
    server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["schub"]
    assert server["env"]["SCHUB_LAB_AGENT_ENGINE"] == "claude" and server["env"]["SCHUB_GOAL_PROJECT"] == "p"
    assert server["env"]["SCHUB_LAB_AGENT_SESSION"] == argv[argv.index("--session-id") + 1]
    assert json.loads((Goal(lab, "p").state / "intent.json").read_text())["job_id"] == successor
    guard = lab.bench_dir / "guard" / "claude"
    assert guard.stat().st_mode & 0o111 and "schub.bench.engines.guard" in guard.read_text()


def test_the_next_slice_resumes_the_same_session(lab: Settings, cluster: FakeCluster) -> None:
    first, _ = start_and_run(lab, cluster)
    _, result = next_slice(lab, cluster, first)
    new, resumed = [c["argv"] for c in engine_calls()]
    assert result == "ok" and resumed[resumed.index("--resume") + 1] == new[new.index("--session-id") + 1]
    assert Goal(lab, "p").turns() == 2


def test_waiting_on_jobs_calls_no_model(lab: Settings, cluster: FakeCluster) -> None:
    first, _ = start_and_run(lab, cluster)
    cluster.jobs["777"], cluster.names["777"] = "PENDING", "schub-cell-p-c0009"
    CheckpointStore(lab.projects_dir / "p").write("waiting", next_action="read the QC table",
                                                  waiting_jobs=[WaitingJob(job_id="777")])
    second, result = next_slice(lab, cluster, first)
    assert result == "waiting" and len(engine_calls()) == 1
    assert successor_of(cluster, second)  # the chain goes on without spending a turn
    cluster.jobs["777"] = "COMPLETED"
    _, result = next_slice(lab, cluster, second)
    assert result == "ok" and len(engine_calls()) == 2


def test_a_usage_limit_hands_the_turn_to_the_other_engine(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE", "limit")
    first, result = start_and_run(lab, cluster)
    assert result == "ok"
    claude, codex = engine_calls()
    assert (claude["engine"], codex["engine"]) == ("claude", "codex")
    assert "This is a new session" not in codex["argv"][-1]  # claude was refused: nothing to hand over yet
    assert not Cooldown(lab.bench_dir / "engine-cooldown.json").ready("claude")
    assert Goal(lab, "p").turns() == 1 and "claude" not in Goal(lab, "p").sessions()  # a refused turn is free
    notes = incidents(lab)
    assert any("claude hit its usage limit" in n and "other engine takes over" in n for n in notes)
    monkeypatch.setenv("FAKE_CLAUDE", "ok")
    _, result = next_slice(lab, cluster, first)
    third = engine_calls()[-1]
    assert result == "ok" and third["engine"] == "codex" and third["argv"][:2] == ["exec", "resume"]


def test_the_owners_policy_is_the_limit(lab: Settings, cluster: FakeCluster) -> None:
    path = lab.bench_dir / "engine-policy.json"
    engine_policy.set_mode(path, "codex-only")
    with pytest.raises(PolicyError, match="allows codex"):
        goal_agent.start(lab, Slurm(cluster), "p", GOAL.replace("engine: auto", "engine: claude"))
    first, result = start_and_run(lab, cluster)
    assert result == "ok" and [c["engine"] for c in engine_calls()] == ["codex"]
    Goal(lab, "p").write(GOAL.replace("engine: auto", "engine: claude"))  # edited behind the policy's back
    _, result = next_slice(lab, cluster, first)
    assert result == "refused" and len(engine_calls()) == 1
    assert any("cannot run" in n for n in incidents(lab))


def test_stop_complete_and_budget_end_the_chain(lab: Settings, cluster: FakeCluster) -> None:
    first, _ = start_and_run(lab, cluster)
    goal_agent.stop(lab, "p")
    second, result = next_slice(lab, cluster, first)
    assert result == "stopped" and not [j for j in cluster.scripts if cluster.dependencies(j) == f"afterany:{second}"]
    (Goal(lab, "p").folder / "STOP").unlink()
    CheckpointStore(lab.projects_dir / "p").write("complete", reason="table done")
    cluster.jobs[second] = "RUNNING"
    assert Slice(lab, Slurm(cluster), "p", second).run() == "done"
    CheckpointStore(lab.projects_dir / "p").write("active", next_action="more")
    Goal(lab, "p").write(GOAL.replace("max_turns: 5", "max_turns: 1"))
    assert Slice(lab, Slurm(cluster), "p", second).run() == "budget"
    assert CheckpointStore(lab.projects_dir / "p").read().disposition == "blocked"


def test_a_lost_session_starts_again_from_the_hand_over(lab: Settings, cluster: FakeCluster, tmp_path: Path,
                                                         monkeypatch) -> None:
    first, _ = start_and_run(lab, cluster)
    script = tmp_path / "claude-script"
    script.write_text("missing\nok\n")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    _, result = next_slice(lab, cluster, first)
    _, lost, fresh = [c["argv"] for c in engine_calls()]
    assert result == "ok" and "--resume" in lost and "--session-id" in fresh
    assert "This is a new session" in fresh[fresh.index("-p") + 1]
    assert Goal(lab, "p").sessions()["claude"]["session_id"] == fresh[fresh.index("--session-id") + 1]


def test_a_duplicate_slice_defers_and_the_watchdog_revives_a_broken_chain(lab: Settings,
                                                                           cluster: FakeCluster) -> None:
    first, _ = start_and_run(lab, cluster)
    successor = successor_of(cluster, first)
    cluster.jobs[first] = "COMPLETED"
    duplicate = goal_agent.Slice(lab, Slurm(cluster), "p", "9999")
    cluster.jobs["9999"], cluster.names["9999"] = "RUNNING", Goal(lab, "p").job_name
    assert duplicate.run() == "deferred" and len(engine_calls()) == 1
    cluster.jobs["9999"] = cluster.jobs[successor] = "CANCELLED"  # the chain is broken
    [revived] = revive(lab, Slurm(cluster))
    assert revived.startswith("p (job ") and revive(lab, Slurm(cluster)) == []


def test_the_lab_agents_cells_are_marked_as_its_own() -> None:
    env = {"SCHUB_LAB_AGENT_ENGINE": "codex", "SCHUB_LAB_AGENT_MODEL": "gpt-x", "SCHUB_LAB_AGENT_SESSION": "t-1"}
    actor = actor_for("codex-mcp-client", "0.155", env)
    assert (actor.kind, actor.engine, actor.model, actor.session_id) == ("lab_agent", "codex", "gpt-x", "t-1")
    assert actor_for("claude-code", "2.1", {}).kind == "chat"


def test_a_limit_after_real_work_counts_and_the_other_engine_gets_what_is_left(lab: Settings, cluster: FakeCluster,
                                                                               monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE", "limitwork")
    first = goal_agent.start(lab, Slurm(cluster), "p", GOAL)
    cluster.jobs[first] = "RUNNING"
    clock = {"t": 0.0}
    slice_ = Slice(lab, Slurm(cluster), "p", first, clock=lambda: clock["t"])
    real_turn = slice_.turn

    def turn(engine, config, policy, rotated=False):
        clock["t"] += 22 * 60  # claude worked 22 of the slice's 30 minutes before its limit
        return real_turn(engine, config, policy, rotated)

    slice_.turn = turn
    assert slice_.run() == "usage_limited"  # 8 minutes left: less than a turn needs, codex waits
    assert Goal(lab, "p").turns() == 1 and Goal(lab, "p").sessions()["claude"]["session_id"]
    assert [c["engine"] for c in engine_calls()] == ["claude"]
    assert "no_time_for_other_engine" in [e["event"] for e in Goal(lab, "p").events()]


def test_restarting_a_finished_goal_opens_it_again(lab: Settings, cluster: FakeCluster) -> None:
    first, _ = start_and_run(lab, cluster)
    CheckpointStore(lab.projects_dir / "p").write("blocked", reason="budget")
    cluster.jobs = {k: "COMPLETED" for k in cluster.jobs}
    again = goal_agent.start(lab, Slurm(cluster), "p", GOAL)  # the same objective: budget and sessions stay
    assert CheckpointStore(lab.projects_dir / "p").read().disposition == "active" and Goal(lab, "p").turns() == 1
    cluster.jobs[again] = "RUNNING"
    assert Slice(lab, Slurm(cluster), "p", again).run() == "ok"
    cluster.jobs = {k: "COMPLETED" for k in cluster.jobs}
    goal_agent.start(lab, Slurm(cluster), "p", GOAL.replace("QC it.", "QC it, then compare to RPE1."))
    assert Goal(lab, "p").turns() == 0 and Goal(lab, "p").sessions() == {}  # a new objective starts fresh
    assert list((Goal(lab, "p").state / "archive").iterdir())


def test_broken_files_do_not_break_the_chain(lab: Settings, cluster: FakeCluster) -> None:
    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "mixed", weekly_turns=50)
    (lab.bench_dir / "engine-usage.jsonl").write_text('{"at": "2026-09-2\nnot json\n')
    first, result = start_and_run(lab, cluster)
    assert result == "ok"
    (lab.bench_dir / "engine-policy.json").write_text('{"grants": []}')
    second, result = next_slice(lab, cluster, first)
    assert result == "refused" and successor_of(cluster, second)
    assert CheckpointStore(lab.projects_dir / "p").read().disposition == "blocked"  # once, not every slice
    cluster.jobs[second] = "COMPLETED"
    third = successor_of(cluster, second)
    cluster.jobs[third] = "RUNNING"
    assert Slice(lab, Slurm(cluster), "p", third).run() == "done"


def test_nested_projects_and_similar_names_have_their_own_slices(lab: Settings, cluster: FakeCluster) -> None:
    ProjectStore(lab).create("a-b")
    ProjectStore(lab).create("a")
    ProjectStore(lab).create("a/b")
    assert Goal(lab, "a-b").job_name != Goal(lab, "a/b").job_name
    goal_agent.start(lab, Slurm(cluster), "a/b", GOAL)
    goal_agent.start(lab, Slurm(cluster), "a-b", GOAL)
    assert len(cluster.jobs) == 2  # neither start mistook the other's slice for its own
    assert set(goal_agent.active_goals(lab)) >= {"a/b", "a-b"}
    cluster.jobs = {k: "CANCELLED" for k in cluster.jobs}
    assert len(revive(lab, Slurm(cluster))) >= 2


def test_a_second_copy_of_a_running_slice_leaves_quietly(lab: Settings, cluster: FakeCluster) -> None:
    from schub.bench.goal_agent import held

    first = goal_agent.start(lab, Slurm(cluster), "p", GOAL)
    cluster.jobs[first] = "RUNNING"
    twin = "4242"
    cluster.jobs[twin], cluster.names[twin] = "RUNNING", Goal(lab, "p").job_name
    with held(Goal(lab, "p"), Slurm(cluster), first):
        assert Slice(lab, Slurm(cluster), "p", twin).run() == "busy"
    assert not engine_calls()


def test_claude_stands_down_above_the_owners_weekly_ceiling(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "mixed", claude_weekly_ceiling=0.8)
    monkeypatch.setenv("FAKE_CLAUDE_WEEK", "0.82")  # this turn is allowed; it reports the window above the ceiling
    first, result = start_and_run(lab, cluster)
    assert result == "ok" and [c["engine"] for c in engine_calls()] == ["claude"]
    _, result = next_slice(lab, cluster, first)
    assert result == "ok" and engine_calls()[-1]["engine"] == "codex"  # mixed: the other engine carries the work
    events = [e["event"] for e in Goal(lab, "p").events(100)]
    assert "claude_above_weekly_ceiling" in events
