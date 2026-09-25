"""The lab agent's guards learned from the pilot owner's own agents (VCC2026 autochallenger) and the
test root: a broken login, a spent budget, a finished goal's queue, jobs Slurm never starts, a
session too long to resume."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schub.bench import goal_agent
from schub.bench.checkpoint import CheckpointStore
from schub.bench.clock import stamp
from schub.bench.engines import policy as engine_policy
from schub.bench.engines.cooldown import Cooldown
from schub.bench.goal import Goal
from schub.bench.goal_agent import Slice
from schub.bench.inbox import Inbox
from schub.bench.models import Actor, CellRequest, WaitingJob
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster
from tests.test_bench_goal_agent import (GOAL, engine_calls, incidents, lab, next_slice,  # noqa: F401 - lab
                                         start_and_run, successor_of)


def cooldown(settings: Settings) -> Cooldown:
    return Cooldown(settings.bench_dir / "engine-cooldown.json")


def an_hour_later(settings: Settings, engine: str) -> None:
    """The next slice comes about an hour later (pace_minutes): the last failure is that old."""
    path = settings.bench_dir / "engine-cooldown.json"
    state = json.loads(path.read_text())
    last = datetime.fromisoformat(state[f"{engine}:failures"]["last"]) - timedelta(hours=1)
    state[f"{engine}:failures"]["last"] = last.isoformat(timespec="seconds")
    path.write_text(json.dumps(state))


def test_a_broken_login_is_not_counted_and_pauses_the_engine(lab: Settings, cluster: FakeCluster,
                                                             monkeypatch) -> None:
    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "claude-only")
    monkeypatch.setenv("FAKE_CLAUDE", "broken")
    first, result = start_and_run(lab, cluster)
    assert result == "failed" and Goal(lab, "p").turns() == 0
    assert incidents(lab) == [] and cooldown(lab).ready("claude")  # one failure may be a passing glitch
    an_hour_later(lab, "claude")
    second, result = next_slice(lab, cluster, first)
    assert result == "failed" and Goal(lab, "p").turns() == 0
    [incident] = incidents(lab)
    assert "2 times in a row" in incident and "retry agents" in incident and "Invalid API key" in incident
    assert cooldown(lab).kind("claude") == "failing" and not cooldown(lab).ready("claude")
    _, result = next_slice(lab, cluster, second)
    assert result == "paused" and len(engine_calls()) == 2  # no third call while paused
    cooldown(lab).clear("claude")  # what the engine probe does once Claude answers again
    assert cooldown(lab).ready("claude") and cooldown(lab).failing("claude", "x") == (1, None)


def test_a_failing_engine_hands_the_turn_to_the_other_one(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE", "broken")
    _, result = start_and_run(lab, cluster)
    assert result == "ok" and [c["engine"] for c in engine_calls()] == ["claude", "codex"]
    assert Goal(lab, "p").turns() == 1


def test_a_failure_after_real_work_counts_and_says_so(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "claude-only")
    monkeypatch.setenv("FAKE_CLAUDE", "failwork")
    _, result = start_and_run(lab, cluster)
    assert result == "failed" and Goal(lab, "p").turns() == 1
    [incident] = incidents(lab)
    assert "turn failed" in incident and "API Error: 500" in incident
    finished = [e for e in Goal(lab, "p").events() if e["event"] == "turn_finished"]
    assert finished[-1]["worked"] is True


def test_a_spent_budget_arms_no_successor(lab: Settings, cluster: FakeCluster) -> None:
    first = goal_agent.start(lab, Slurm(cluster), "p", GOAL.replace("max_turns: 5", "max_turns: 1"))
    cluster.jobs[first] = "RUNNING"
    assert Slice(lab, Slurm(cluster), "p", first).run() == "ok"
    second, result = next_slice(lab, cluster, first)
    assert result == "budget" and CheckpointStore(lab.projects_dir / "p").read().disposition == "blocked"
    assert not [j for j in cluster.scripts if cluster.dependencies(j) == f"afterany:{second}"]


def queue(settings: Settings, project: str, cid: str, actor: Actor) -> None:
    Inbox(settings.bench_dir).submit(CellRequest(project=project, cid=cid, code="1", why="w", expect="e",
                                                 created=stamp(), actor=actor))


def test_a_finished_goal_withdraws_only_its_own_queued_research(lab: Settings, cluster: FakeCluster) -> None:
    ProjectStore(lab).create("q")
    queue(lab, "p", "c0001", Actor(kind="lab_agent", engine="codex"))
    queue(lab, "p", "c0002", Actor(kind="chat"))  # the student's own
    queue(lab, "p", "c0003", Actor(kind="lab_agent", role="writer"))  # the report writer's
    queue(lab, "q", "c0001", Actor(kind="lab_agent"))  # another project's lab agent
    first, _ = start_and_run(lab, cluster)
    CheckpointStore(lab.projects_dir / "p").write("blocked", reason="needs the student")
    _, result = next_slice(lab, cluster, first)
    inbox = Inbox(lab.bench_dir)
    assert result == "done"
    assert sorted((r.project, r.cid) for r in inbox.pending()) == [("p", "c0002"), ("p", "c0003"), ("q", "c0001")]
    assert "goal is blocked" in inbox.rejected_reason("p", "c0001")
    assert any(e["event"] == "withdrawn" and e["cells"] == ["c0001"] for e in Goal(lab, "p").events())


def test_a_goal_that_ends_in_its_own_turn_withdraws_at_once(lab: Settings, cluster: FakeCluster, tmp_path: Path,
                                                            monkeypatch) -> None:
    hook = tmp_path / "hook.py"  # the agent queues a cell, then hands over as blocked
    hook.write_text("import json as _j\nfrom pathlib import Path as _P\n"
                    "(_P(os.environ['HOOK_PROJECT_DIR']) / 'journal' / 'checkpoint.json').write_text(_j.dumps("
                    "{'disposition': 'blocked', 'updated': '2026-09-24T10:00:00.000+00:00', 'reason': 'stuck'}))\n")
    monkeypatch.setenv("FAKE_HOOK", str(hook))
    monkeypatch.setenv("HOOK_PROJECT_DIR", str(lab.projects_dir / "p"))
    queue(lab, "p", "c0001", Actor(kind="lab_agent"))
    _, result = start_and_run(lab, cluster)
    assert result == "ok" and Inbox(lab.bench_dir).pending() == []


def test_a_job_slurm_will_never_start_wakes_the_model(lab: Settings, cluster: FakeCluster) -> None:
    first, _ = start_and_run(lab, cluster)
    cluster.jobs["777"], cluster.names["777"] = "PENDING", "schub-cell-p-c0009"
    cluster.reasons["777"] = "(DependencyNeverSatisfied)"
    CheckpointStore(lab.projects_dir / "p").write("waiting", next_action="read the QC table",
                                                  waiting_jobs=[WaitingJob(job_id="777")])
    _, result = next_slice(lab, cluster, first)
    assert result == "ok" and len(engine_calls()) == 2
    prompt = engine_calls()[-1]["argv"][engine_calls()[-1]["argv"].index("-p") + 1]
    assert "may never start" in prompt and "777 (DependencyNeverSatisfied)" in prompt and "cluster()" in prompt


@pytest.mark.parametrize("reason", ["(QOSMaxJobsPerUserLimit)", "(JobHeldUser)"])  # a free slot; the student's own hold
def test_an_ordinary_wait_stays_quiet(lab: Settings, cluster: FakeCluster, reason: str) -> None:
    first, _ = start_and_run(lab, cluster)
    cluster.jobs["777"], cluster.names["777"] = "PENDING", "schub-cell-p-c0009"
    cluster.reasons["777"] = reason
    CheckpointStore(lab.projects_dir / "p").write("waiting", next_action="x", waiting_jobs=[WaitingJob(job_id="777")])
    second, result = next_slice(lab, cluster, first)
    assert result == "waiting" and len(engine_calls()) == 1 and successor_of(cluster, second)


def test_a_session_too_long_to_resume_starts_a_new_one(lab: Settings, cluster: FakeCluster, tmp_path: Path,
                                                       monkeypatch) -> None:
    first, _ = start_and_run(lab, cluster)
    script = tmp_path / "claude-script"
    script.write_text("toolong\nok\n")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    _, result = next_slice(lab, cluster, first)
    _, full, fresh = [c["argv"] for c in engine_calls()]
    assert result == "ok" and "--resume" in full and "--session-id" in fresh
    assert "This is a new session" in fresh[fresh.index("-p") + 1]


def test_a_codex_turn_that_called_tools_before_failing_counts(lab: Settings, cluster: FakeCluster,
                                                              monkeypatch) -> None:
    """Codex reports no turns or cost: its tool calls are the evidence of work (not a sign-in problem)."""
    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "codex-only")
    monkeypatch.setenv("FAKE_CODEX", "workfail")
    _, result = start_and_run(lab, cluster)
    assert result == "failed" and Goal(lab, "p").turns() == 1
    [incident] = incidents(lab)
    assert "turn failed" in incident and "sign in" not in incident and cooldown(lab).ready("codex")


def test_a_session_that_fills_up_after_work_counts_then_starts_anew(lab: Settings, cluster: FakeCluster,
                                                                    tmp_path: Path, monkeypatch) -> None:
    first, _ = start_and_run(lab, cluster)
    script = tmp_path / "claude-script"
    script.write_text("fullwork\nok\n")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    _, result = next_slice(lab, cluster, first)
    assert result == "ok" and Goal(lab, "p").turns() == 3  # the first slice, the full one, the fresh one


def test_a_session_that_cannot_start_again_says_so(lab: Settings, cluster: FakeCluster, tmp_path: Path,
                                                   monkeypatch) -> None:
    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "claude-only")
    first, _ = start_and_run(lab, cluster)
    script = tmp_path / "claude-script"
    script.write_text("toolong\ntoolong\n")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    _, result = next_slice(lab, cluster, first)
    assert result == "session_missing" and any("fresh conversation" in text for text in incidents(lab))


def fail_twice(lab: Settings, cluster: FakeCluster, monkeypatch) -> str:
    """Claude-only, its login broken: two slices an hour apart; returns the job of the second."""
    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "claude-only")
    monkeypatch.setenv("FAKE_CLAUDE", "broken")
    first, _ = start_and_run(lab, cluster)
    an_hour_later(lab, "claude")
    second, _ = next_slice(lab, cluster, first)
    assert not cooldown(lab).ready("claude")
    return second


def test_a_new_sign_in_ends_the_pause_at_the_next_slice(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    second = fail_twice(lab, cluster, monkeypatch)
    state = json.loads((lab.bench_dir / "engine-cooldown.json").read_text())
    state["claude"]["until"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    (lab.bench_dir / "engine-cooldown.json").write_text(json.dumps(state))
    an_hour_later(lab, "claude")
    third, _ = next_slice(lab, cluster, second)  # the third failure: paused for 6 hours
    assert cooldown(lab).until("claude") > datetime.now(timezone.utc) + timedelta(hours=5)
    fourth, result = next_slice(lab, cluster, third)  # paused: the next look is one pace away, not six hours
    [delay] = [u for u in cluster.updates if "StartTime" in u]
    assert result == "paused" and delay["StartTime"] in ("now+60minutes", "now+61minutes")
    monkeypatch.setenv("FAKE_CLAUDE", "ok")
    monkeypatch.setattr(goal_agent, "sign_in_fingerprint", lambda engine: "a-new-login")  # the student signed in
    _, result = next_slice(lab, cluster, fourth)
    assert result == "ok" and cooldown(lab).ready("claude") and Goal(lab, "p").turns() == 1
    assert any(e["event"] == "signed_in_again" for e in Goal(lab, "p").events())


def test_starting_the_goal_again_tries_the_engine_at_once(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    second = fail_twice(lab, cluster, monkeypatch)
    waiting = successor_of(cluster, second)
    cluster.jobs[second] = "COMPLETED"
    cluster.reasons[waiting] = "(BeginTime)"  # put off until the pause ends
    goal_agent.start(lab, Slurm(cluster), "p", GOAL)
    assert cooldown(lab).ready("claude")
    assert {"JobId": waiting, "StartTime": "now+1minutes"}.items() <= cluster.updates[-1].items()


def test_a_goal_keeps_a_watchdog(lab: Settings, cluster: FakeCluster) -> None:
    goal_agent.start(lab, Slurm(cluster), "p", GOAL)
    assert "schub-bench-watchdog" in cluster.names.values()


def test_a_wait_of_more_than_a_day_wakes_the_model(lab: Settings, cluster: FakeCluster) -> None:
    first, _ = start_and_run(lab, cluster)
    cluster.jobs["777"], cluster.names["777"] = "PENDING", "schub-cell-p-c0009"
    cluster.reasons["777"] = "(Dependency)"  # behind a parent that will never run
    store = CheckpointStore(lab.projects_dir / "p")
    store.write("waiting", next_action="x", waiting_jobs=[WaitingJob(job_id="777")])
    path = lab.projects_dir / "p" / "journal" / "checkpoint.json"
    data = json.loads(path.read_text())
    data["updated"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    path.write_text(json.dumps(data))
    second, result = next_slice(lab, cluster, first)
    prompt = engine_calls()[-1]["argv"][engine_calls()[-1]["argv"].index("-p") + 1]
    assert result == "ok" and "pending for over 24 hours" in prompt and "hand over as \"waiting\" again" in prompt
    cluster.jobs["777"] = "RUNNING"  # a long job that runs: nothing to look at, the wait goes on
    store.write("waiting", next_action="x", waiting_jobs=[WaitingJob(job_id="777")])
    data = json.loads(path.read_text())
    data["updated"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    path.write_text(json.dumps(data))
    calls = len(engine_calls())
    _, result = next_slice(lab, cluster, second)
    assert result == "waiting" and len(engine_calls()) == calls


def test_a_hanging_engine_is_treated_like_a_failing_one(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    from schub.bench.engines.base import Outcome

    engine_policy.set_mode(lab.bench_dir / "engine-policy.json", "claude-only")
    first, _ = start_and_run(lab, cluster)
    hung = Outcome("claude", "timed_out", error="")  # killed at the deadline without a tool call
    monkeypatch.setattr(goal_agent.ADAPTERS["claude"], "run", lambda *a, **k: hung)
    an = Goal(lab, "p").turns()
    _, result = next_slice(lab, cluster, first)
    assert result == "timed_out" and Goal(lab, "p").turns() == an
    assert json.loads((lab.bench_dir / "engine-cooldown.json").read_text())["claude:failures"]["count"] == 1


def test_no_new_session_when_the_slice_is_nearly_over(lab: Settings, cluster: FakeCluster, tmp_path: Path,
                                                      monkeypatch) -> None:
    first, _ = start_and_run(lab, cluster)
    script = tmp_path / "claude-script"
    script.write_text("fullwork\nok\n")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    monkeypatch.setattr(goal_agent.Slice, "remaining_s", lambda self, config: 120)
    _, result = next_slice(lab, cluster, first)
    assert result == "session_missing" and len(engine_calls()) == 2  # the next slice starts the new session
    assert Goal(lab, "p").sessions().get("claude") is None


def test_a_limit_without_a_named_reset_pauses_longer_each_time_and_says_so_once(tmp_path: Path) -> None:
    clock = {"now": datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)}
    store = Cooldown(tmp_path / "cooldown.json", now=lambda: clock["now"])
    spend_cap = "You hit your spend cap set by the owner of your workspace."
    first = store.mark("codex", spend_cap)
    assert first == clock["now"] + timedelta(hours=5) and store.streak("codex") == 1
    clock["now"] = first + timedelta(minutes=10)  # tried again when the pause ended: the same cap
    second = store.mark("codex", spend_cap)
    assert second == clock["now"] + timedelta(hours=10) and store.streak("codex") == 2
    clock["now"] = second + timedelta(days=2)  # much later: a new episode
    assert store.mark("codex", spend_cap) == clock["now"] + timedelta(hours=5) and store.streak("codex") == 1
