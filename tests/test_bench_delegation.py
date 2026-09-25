"""Handing a task to the lab agent (delegate / delegation), and the free lab agent: its own tools in the project
folder, sandboxed, and its own actions recorded in the journal after each turn."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schub.bench import agent_record, goal_agent
from schub.bench.delegation import DelegationError, delegate, delegation
from schub.bench.engines.base import McpServer, Outcome, Turn
from schub.bench.engines.claude import Claude
from schub.bench.engines.codex import Codex
from schub.bench.goal import Goal, parse_goal
from schub.bench.journal import Journal
from schub.bench.models import Actor, FileChange
from schub.config import Settings
from schub.slurm import Slurm
from tests.conftest import FakeCluster
from tests.test_bench_goal_agent import engine_calls, lab, start_and_run  # noqa: F401 - lab

OBJECTIVE = "Build the K562 perturbation table from the scPerturb file, QC it and plot knockdown per target."


def test_a_chat_hands_a_task_over_and_follows_it(lab: Settings, cluster: FakeCluster) -> None:
    answer = delegate(lab, Slurm(cluster), "p", OBJECTIVE, deliverables="table.csv, knockdown.png", max_turns=12,
                      actor=Actor(kind="chat", client="codex"))
    config = Goal(lab, "p").config()
    assert answer.state == "started" and answer.job and answer.max_turns == 12 and answer.mode == "free"
    assert config.mode == "free" and config.max_turns == 12 and "Deliverables: table.csv" in config.objective
    assert "handed over by the student's assistant" in (Goal(lab, "p").folder / "goal.md").read_text()
    assert "handed over" not in config.objective
    assert cluster.names[answer.job].startswith("schub-goal-p")
    assert delegation(lab, Slurm(cluster), "p").state == "queued"
    cluster.jobs[answer.job] = "RUNNING"
    assert delegation(lab, Slurm(cluster), "p").state == "working"
    with pytest.raises(DelegationError, match="already working"):  # a second task must not silently replace it
        delegate(lab, Slurm(cluster), "p", OBJECTIVE + " Also plot it.", actor=Actor())
    with pytest.raises(DelegationError, match="does not stop lab agents"):
        delegation(lab, Slurm(cluster), "p", stop=True, actor=Actor(kind="lab_agent", engine="codex"))
    stopped = delegation(lab, Slurm(cluster), "p", stop=True, actor=Actor())
    assert stopped.state == "stopped" and (Goal(lab, "p").folder / "STOP").exists()


def test_handing_the_same_task_over_again_keeps_its_budget(lab: Settings, cluster: FakeCluster) -> None:
    delegate(lab, Slurm(cluster), "p", OBJECTIVE, actor=Actor())
    Goal(lab, "p").count_turn("claude", "1")
    cluster.jobs = {k: "COMPLETED" for k in cluster.jobs}  # its slice ended; the goal is not running now
    delegate(lab, Slurm(cluster), "p", OBJECTIVE, actor=Actor())
    assert Goal(lab, "p").turns() == 1  # the same objective: not a new goal
    replaced = delegate(lab, Slurm(cluster), "p", OBJECTIVE + " Now for RPE1.", actor=Actor(), replace=True)
    assert replaced.state == "started" and Goal(lab, "p").turns() == 0  # a new objective starts over


def test_what_a_delegation_refuses(lab: Settings, cluster: FakeCluster) -> None:
    with pytest.raises(DelegationError, match="never hands work to another"):
        delegate(lab, Slurm(cluster), "p", OBJECTIVE, actor=Actor(kind="lab_agent", engine="claude"))
    with pytest.raises(DelegationError, match="in full"):
        delegate(lab, Slurm(cluster), "p", "do it", actor=Actor())
    with pytest.raises(DelegationError, match="max_turns"):
        delegate(lab, Slurm(cluster), "p", OBJECTIVE, max_turns=0)
    with pytest.raises(DelegationError, match="does not exist"):
        delegate(lab, Slurm(cluster), "nope", OBJECTIVE)
    with pytest.raises(DelegationError):
        delegation(lab, Slurm(cluster), "../escaped", stop=True)
    assert not (lab.root / "escaped").exists()
    assert delegation(lab, Slurm(cluster), "p").state == "not started" and cluster.jobs == {}


def test_goal_files_know_the_mode() -> None:
    assert parse_goal("an objective long enough").mode == "bench"  # goals from before the free mode keep theirs
    assert parse_goal("---\nmode: free\n---\nan objective").mode == "free"
    assert parse_goal("---\nmode: bench\n---\nan objective").mode == "bench"
    with pytest.raises(Exception, match="mode must be"):
        parse_goal("---\nmode: wild\n---\nan objective")


def free_turn(**kwargs) -> Turn:
    return Turn(prompt="work", cwd=Path("/p"), run_dir=Path("/r"), timeout_s=60,
                mcp=McpServer("python", ("-m", "schub.cli", "mcp"), ()), free=True, **kwargs)


def test_a_free_claude_has_its_tools_in_a_sandbox_that_hides_keys() -> None:
    argv = Claude().argv("claude", free_turn())
    assert "--tools" not in argv and argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    allowed = argv[argv.index("--allowedTools") + 1:]
    assert {"Bash", "Task", "mcp__schub__*"} <= set(allowed)
    assert not {"Edit", "Write", "MultiEdit", "NotebookEdit"} & set(allowed)  # a bare name would allow edits anywhere
    settings = json.loads(argv[argv.index("--settings") + 1])
    sandbox, ssh = settings["sandbox"], str(Path.home() / ".ssh")
    assert sandbox["enabled"] and sandbox["failIfUnavailable"] and not sandbox["allowUnsandboxedCommands"]
    assert sandbox["network"]["allowedDomains"] == ["*"]
    assert ssh in sandbox["filesystem"]["denyRead"] and f"Read({ssh}/**)" in settings["permissions"]["deny"]
    assert str(Path.home() / ".codex" / "auth.json") in sandbox["filesystem"]["denyRead"]
    bench = Claude().argv("claude", Turn(prompt="work", cwd=Path("/r"), run_dir=Path("/r"), timeout_s=60,
                                         mcp=McpServer("python", (), ())))
    assert bench[bench.index("--tools") + 1] == "" and "--settings" not in bench


def test_a_free_codex_writes_only_in_its_folder() -> None:
    argv = Codex().argv("codex", free_turn())
    assert 'default_permissions="schub_free"' in argv and not any(a.startswith("sandbox_mode") for a in argv)
    profile = next(a for a in argv if a.startswith("permissions.schub_free.filesystem"))
    assert '":root" = "read"' in profile and f'"{Path.home() / ".ssh"}" = "none"' in profile
    assert '":workspace_roots" = { "." = "write" }' in profile and "permissions.schub_free.network={ enabled = true }" in argv
    assert "features.shell_tool=false" not in argv and "features.multi_agent=false" not in argv
    assert "features.browser_use=false" in argv and "features.computer_use=false" in argv


def test_a_free_turn_works_in_the_project_and_its_own_work_is_journaled(lab: Settings, cluster: FakeCluster,
                                                                       monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE", "freework")
    first = goal_agent.start(lab, Slurm(cluster), "p", "---\nmode: free\nmax_turns: 3\n---\n" + OBJECTIVE)
    cluster.jobs[first] = "RUNNING"
    result = goal_agent.Slice(lab, Slurm(cluster), "p", first).run()
    [call] = engine_calls()
    assert result == "ok" and Path(call["cwd"]).resolve() == (lab.projects_dir / "p" / "work").resolve()
    assert "your own tools" in call["argv"][call["argv"].index("-p") + 1]
    [cell] = [e for e in Journal(lab.projects_dir / "p", "p").entries(limit=20) if getattr(e, "code", "")]
    assert cell.actor.kind == "lab_agent" and cell.status == "ok" and "$ ls data | head" in cell.code
    assert "# edit: notes/plan.md" in cell.code and "1 file reads or searches, 1 sc-hub tool calls" in cell.code
    assert cell.outputs[0].text == "$ ls data | head\nkang.h5ad"
    assert [(f.path, f.change) for f in cell.files] == [("work/notes/plan.md", "created")]
    assert any(e["event"] == "own_work_recorded" for e in Goal(lab, "p").events())


def test_a_bench_goal_keeps_to_the_sc_hub_tools(lab: Settings, cluster: FakeCluster, monkeypatch) -> None:
    from tests.test_bench_goal_agent import GOAL

    monkeypatch.setenv("FAKE_CLAUDE", "freework")
    first = goal_agent.start(lab, Slurm(cluster), "p", GOAL.replace("pace_minutes: 60", "pace_minutes: 60\nmode: bench"))
    cluster.jobs[first] = "RUNNING"
    goal_agent.Slice(lab, Slurm(cluster), "p", first).run()
    [call] = engine_calls()
    assert "--tools" in call["argv"] and Path(call["cwd"]).name == "claude"  # its run folder, not the project
    assert not any(e["event"] == "own_work_recorded" for e in Goal(lab, "p").events())


def test_codex_transcripts_are_read_too() -> None:
    stdout = "\n".join(json.dumps(e) for e in (
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q", "aggregated_output": "1 failed",
                                            "exit_code": 1}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "src/a.py", "kind": "update"}]}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "schub", "tool": "run"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
    turn = agent_record.parse("codex", stdout)
    assert [(a.kind, a.what, a.failed) for a in turn.actions] == [("shell", "pytest -q", True),
                                                                 ("edit", "src/a.py (update)", False)]
    assert turn.bench_calls == 1


def test_a_turn_that_only_used_the_sc_hub_tools_adds_no_cell(settings: Settings, tmp_path: Path) -> None:
    from schub.projects import ProjectStore

    ProjectStore(settings).create("q")
    journal = Journal(settings.projects_dir / "q", "q")
    only_bench = agent_record.Turn(actions=(), looks=3, bench_calls=5)
    outcome = Outcome("claude", "ok", text="done")
    assert agent_record.record(journal, only_bench, (), outcome, Actor(kind="lab_agent"), tmp_path / "t", "now") is None
    long = agent_record.Turn(actions=(agent_record.Action("shell", "cat big", "x" * 5000),), looks=0, bench_calls=0)
    cid = agent_record.record(journal, long, (FileChange(path="a", change="deleted"),), Outcome("claude", "failed",
                              error="boom"), Actor(kind="lab_agent", engine="claude"), tmp_path / "t", "now")
    cell = journal.cell(cid)
    assert cell.status == "error" and "boom" in cell.message and cell.outputs[0].truncated == 3500
    assert "characters left out" in cell.outputs[0].text and cell.outputs[0].text.endswith("x" * 750)
