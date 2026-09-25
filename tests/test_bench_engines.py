"""Engine policy, pauses after usage limits, both adapters and the guard, on fake CLIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schub.bench.engines import policy as engine_policy
from schub.bench.engines.base import GUARD_DIR, McpServer, Turn
from schub.bench.engines.claude import Claude
from schub.bench.engines.codex import Codex
from schub.bench.engines.cooldown import Cooldown, is_limit, parse_reset, reset_hint
from schub.bench.engines.guard import REFUSED, main as guard_main, pinned
from schub.bench.engines.policy import EnginePolicy, PolicyError, load, order
from schub.bench.engines.probe import probe, summary
from schub.config import Settings
from tests.fake_engines import calls, install

NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    paths = install(tmp_path / "fakebin")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("SCHUB_CLAUDE_BIN", str(paths["claude"]))
    monkeypatch.setenv("SCHUB_CODEX_BIN", str(paths["codex"]))
    return paths


def turn(tmp_path: Path, **kwargs) -> Turn:
    values = {"prompt": "do the next step", "cwd": tmp_path, "run_dir": tmp_path / "run", "timeout_s": 30,
              "mcp": McpServer("python", ("-m", "schub.cli", "mcp"), (("SCHUB_LAB_AGENT_ENGINE", "x"),))}
    return Turn(**{**values, **kwargs})


# ---- policy -------------------------------------------------------------------------


def test_the_default_policy_is_mixed_with_claude_first(tmp_path: Path) -> None:
    policy = load(tmp_path / "missing.json")
    assert (policy.mode, policy.allowed("p")) == ("mixed", ("claude", "codex"))
    assert EnginePolicy(mode="codex-only").allowed("p") == ("codex",)
    assert EnginePolicy(mode="claude-only", grants={"p": {"engines": ["codex"]}}).allowed("p") == ("claude", "codex")
    assert EnginePolicy(mode="claude-only", grants={"p": {"engines": ["codex"]}}).allowed("q") == ("claude",)


def test_a_broken_policy_allows_nothing(tmp_path: Path) -> None:
    path = tmp_path / "engine-policy.json"
    for text in ("{not json", '{"mode": "anything"}', '{"mode": "mixed", "sneaky": 1}', "[]",
                 '{"grants": {"p": {"engines": ["gpt"]}}}', '{"claude_model": "x; rm -rf /"}', '{"grants": []}',
                 '{"grants": {"p": "claude"}}'):
        path.write_text(text)
        with pytest.raises(PolicyError):
            load(path)


def test_the_owner_switches_and_grants(tmp_path: Path) -> None:
    path = tmp_path / "engine-policy.json"
    path.write_text("{broken")
    assert engine_policy.set_mode(path, "mixed").mode == "mixed"  # the owner can always repair the file
    engine_policy.set_mode(path, "codex-only", reason="Claude week spent", codex_model="gpt-fake")
    engine_policy.grant(path, "paper", ["claude"], note="one paper needs it")
    policy = load(path)
    assert policy.mode == "codex-only" and policy.codex_model == "gpt-fake" and policy.updated
    assert policy.allowed("paper") == ("codex", "claude") and policy.allowed("other") == ("codex",)
    engine_policy.revoke(path, "paper")
    assert load(path).allowed("paper") == ("codex",)


def test_order_follows_the_goal_within_the_policy() -> None:
    mixed = EnginePolicy()
    assert order(mixed, "p", "auto", lambda e: True) == ["claude", "codex"]
    assert order(mixed, "p", "auto", lambda e: e != "claude") == ["codex"]
    assert order(mixed, "p", "codex", lambda e: True) == ["codex"]
    with pytest.raises(PolicyError, match="allows codex"):
        order(EnginePolicy(mode="codex-only"), "p", "claude", lambda e: True)


# ---- cooldown -----------------------------------------------------------------------


def test_limits_are_recognised_and_resets_parsed() -> None:
    assert is_limit("You've hit your limit · resets 3pm (Asia/Dubai)") and is_limit("Error 429 Too Many Requests")
    assert is_limit("You hit your spend cap set by the owner of your workspace. Ask an owner to increase your "
                    "spend cap to continue.")  # Codex, 2026-09-24, verbatim
    assert not is_limit("KeyError: 'gene'")
    dubai = parse_reset("You've hit your limit · resets 3pm (Asia/Dubai)", NOW)
    assert dubai == datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc)  # 15:00 +04
    assert parse_reset("resets at 7am", NOW) == datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc)  # past: tomorrow
    assert parse_reset("Try again at 2026-09-24T10:30:00Z.", NOW) == datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
    assert parse_reset("try again at 2020-01-01T00:00Z", NOW) is None  # an old date is not a reset
    assert parse_reset("the job finished at 3pm", NOW) is None
    assert parse_reset("resets at 2026-02-30T10:00Z", NOW) is None  # not a date
    assert parse_reset("You hit your usage limit. Try again in 4 days 3 hours.", NOW) == NOW + timedelta(days=4, hours=3)
    assert parse_reset("try again in 45 minutes", NOW) == NOW + timedelta(minutes=45)


def test_a_named_day_is_parsed() -> None:
    """Claude's weekly limit names a day (VCC2026 A164): it used to fall back to a five-hour pause."""
    before = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert parse_reset("You've hit your limit · resets Sep 14, 10pm (Asia/Dubai)", before) == \
        datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)
    assert parse_reset("resets on September 14 2026 at 22:30", before) == datetime(2026, 9, 14, 22, 30,
                                                                                    tzinfo=timezone.utc)
    assert parse_reset("resets Jan 2, 9am", datetime(2026, 12, 30, tzinfo=timezone.utc)) == \
        datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc)  # read in late December: next year's
    assert parse_reset("resets Sep 12, 10pm", before) is None  # just passed: stale, not a reset a year away
    assert parse_reset("resets Sep 14 2025, 10pm", before) is None  # a day gone by is not a reset
    assert parse_reset("You've hit your usage limit. Try again at Sep 20th, 2026 3:05 PM.", before) == \
        datetime(2026, 9, 20, 15, 5, tzinfo=timezone.utc)  # Codex's wording
    assert parse_reset("resets Feb 30, 10pm", before) is None
    assert parse_reset("resets Sep 14, 25pm", before) is None


def test_claudes_own_reset_time_wins(tmp_path: Path) -> None:
    at = datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc)
    refused = {"rate_limit": {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": at.timestamp()}}
    assert reset_hint(refused) == at
    assert reset_hint({"rate_limit": {"status": "allowed", "resetsAt": at.timestamp()}}) is None
    assert reset_hint({"rate_limit": {"status": "rejected", "resetsAt": "soon"}}) is None and reset_hint({}) is None
    cooldown = Cooldown(tmp_path / "cooldown.json", now=lambda: NOW)
    assert cooldown.mark("claude", "You've hit your limit · resets 3pm (Asia/Dubai)", resets=at) == at
    assert cooldown.mark("codex", "resets at 10am", resets=NOW - timedelta(hours=1)) == \
        datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)  # a past hint: the message decides


def test_an_engine_failing_in_a_row_pauses_longer_each_time(tmp_path: Path) -> None:
    clock = {"now": NOW}
    cooldown = Cooldown(tmp_path / "cooldown.json", now=lambda: clock["now"])
    pauses = []
    for _ in range(5):  # one failure per slice, an hour apart
        pauses.append(cooldown.failing("claude", "Invalid API key")[1])
        clock["now"] += timedelta(hours=1)
    start = NOW
    assert pauses == [None, start + timedelta(hours=2), start + timedelta(hours=8), start + timedelta(hours=27),
                      start + timedelta(hours=28)]
    assert cooldown.kind("claude") == "failing" and cooldown.ready("codex")
    cooldown.answered("claude")  # a turn did work: the count starts again
    assert cooldown.failing("claude", "x") == (1, None)


def test_failures_in_one_short_outage_count_once(tmp_path: Path) -> None:
    clock = {"now": NOW}
    cooldown = Cooldown(tmp_path / "cooldown.json", now=lambda: clock["now"])
    assert cooldown.failing("claude", "API Error: 529 Overloaded") == (1, None)
    clock["now"] += timedelta(minutes=5)  # another project's slice, same outage
    assert cooldown.failing("claude", "API Error: 529 Overloaded") == (1, None) and cooldown.ready("claude")
    clock["now"] += timedelta(minutes=40)
    assert cooldown.failing("claude", "API Error: 529 Overloaded") == (2, clock["now"] + timedelta(hours=1))
    cooldown.mark("claude", "usage limit")
    assert cooldown.kind("claude") == "usage limit"


def test_a_paused_engine_is_ready_again_after_its_reset(tmp_path: Path) -> None:
    clock = {"now": NOW}
    cooldown = Cooldown(tmp_path / "cooldown.json", now=lambda: clock["now"])
    until = cooldown.mark("claude", "usage limit reached, resets at 10am")
    assert until == datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc) and not cooldown.ready("claude")
    assert cooldown.ready("codex")
    assert cooldown.mark("codex", "quota exceeded") == datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)
    assert cooldown.mark("gemini", "limit reached, resets at 2099-01-01T00:00Z") == datetime(2026, 10, 2, 8, 0,
                                                                                               tzinfo=timezone.utc)
    clock["now"] = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    assert cooldown.ready("claude") and cooldown.summary(("claude",)) == {"claude": "ready"}


# ---- adapters -----------------------------------------------------------------------


def test_claude_new_session_then_resume(fakes, tmp_path: Path) -> None:
    first = Claude().run(turn(tmp_path, new_session_id="aaaaaaaa-0000-4000-8000-000000000001", model="m1"))
    assert first.status == "ok" and first.session_id == "aaaaaaaa-0000-4000-8000-000000000001"
    assert first.cost_usd == 0.01 and first.text == "did the step"
    second = Claude().run(turn(tmp_path, session_id=first.session_id))
    assert second.status == "ok"
    new, resumed = [c["argv"] for c in calls(Path(os.environ["FAKE_LOG"]))]
    assert new[new.index("--session-id") + 1] == first.session_id and "--resume" not in new
    assert resumed[resumed.index("--resume") + 1] == first.session_id
    assert new[new.index("--tools") + 1] == "" and new[new.index("--model") + 1] == "m1"
    config = json.loads(new[new.index("--mcp-config") + 1])
    assert config["mcpServers"]["schub"]["env"] == {"SCHUB_LAB_AGENT_ENGINE": "x"}
    assert new[new.index("--allowedTools") + 1] == "mcp__schub__*" and "--strict-mcp-config" in new


@pytest.mark.parametrize("mode,status", [("limit", "usage_limited"), ("missing", "session_missing"),
                                         ("garbage", "failed"), ("broken", "failed"), ("failwork", "failed"),
                                         ("toolong", "session_missing")])
def test_claude_failures_are_classified(fakes, tmp_path: Path, monkeypatch, mode: str, status: str) -> None:
    monkeypatch.setenv("FAKE_CLAUDE", mode)
    outcome = Claude().run(turn(tmp_path, session_id="s-1"))
    assert outcome.status == status and outcome.error


def test_codex_thread_resume_and_limit(fakes, tmp_path: Path, monkeypatch) -> None:
    first = Codex().run(turn(tmp_path, model="gpt-fake", effort="high"))
    assert first.status == "ok" and first.session_id == "019a0000-0000-7000-8000-00000000c0de"
    Codex().run(turn(tmp_path, session_id=first.session_id))
    new, resumed = [c["argv"] for c in calls(Path(os.environ["FAKE_LOG"]))]
    assert new[:2] == ["exec", "--json"] and resumed[:3] == ["exec", "resume", first.session_id]
    assert 'sandbox_mode="read-only"' in new and 'model="gpt-fake"' in new and 'model_reasoning_effort="high"' in new
    assert 'mcp_servers.schub.env={ "SCHUB_LAB_AGENT_ENGINE" = "x" }' in new
    assert "features.shell_tool=false" in new and "features.multi_agent=false" in new  # MCP tools only
    assert 'mcp_servers.schub.default_tools_approval_mode="approve"' in new and new[-1] == "do the next step"
    monkeypatch.setenv("FAKE_CODEX", "limit")
    limited = Codex().run(turn(tmp_path, session_id=first.session_id))
    assert limited.status == "usage_limited" and "2099-01-01" in limited.error
    monkeypatch.setenv("FAKE_CODEX", "missing")
    assert Codex().run(turn(tmp_path, session_id="gone")).status == "session_missing"


def test_a_codex_thread_that_no_longer_fits_is_started_again() -> None:
    full = json.dumps({"type": "turn.failed", "error": {"message": "Codex ran out of room in the model's context "
                                                                   "window. Start a new conversation."}})
    assert Codex().parse(full, "", 1).status == "session_missing"
    assert Codex().parse(json.dumps({"type": "error", "message": "context_length_exceeded"}), "", 1).status == \
        "session_missing"


def test_a_turn_past_its_time_is_stopped(fakes, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE", "slow")
    outcome = Claude().run(turn(tmp_path, new_session_id="s-2", timeout_s=1))
    assert outcome.status == "timed_out" and outcome.session_id == "s-2"


# ---- guard --------------------------------------------------------------------------


def test_pinned_replaces_the_callers_model() -> None:
    policy = EnginePolicy(claude_model="opus-x", claude_effort="high", codex_model="gpt-x")
    assert pinned("claude", ["-p", "hi", "--model", "cheap", "--effort=low"], policy) == \
        ["-p", "hi", "--model", "opus-x", "--effort", "high"]
    assert pinned("claude", ["-p", "hi"], EnginePolicy(claude_effort="")) == ["-p", "hi"]
    assert pinned("claude", ["-p", "hi"], EnginePolicy()) == ["-p", "hi", "--effort", "xhigh"]  # the default effort
    assert pinned("codex", ["exec", "-m", "cheap", "-c", 'model="cheap"', "--json", "go"], policy) == \
        ["exec", "-c", 'model="gpt-x"', "-c", 'model_reasoning_effort="xhigh"', "--json", "go"]


def guard_env(settings: Settings, tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "SCHUB_ROOT": str(settings.root), "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "SCHUB_PYTHON": sys.executable}


def test_the_guard_refuses_what_the_policy_forbids(settings: Settings, fakes, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    engine_policy.set_mode(settings.bench_dir / "engine-policy.json", "claude-only", claude_model="opus-x")
    assert guard_main(["codex", "exec", "hi"]) == REFUSED
    done = subprocess.run([str(GUARD_DIR / "claude"), "-p", "Reply with exactly: OK", "--model", "cheap"],
                          env=guard_env(settings, tmp_path), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and json.loads(done.stdout.splitlines()[-1])["result"] == "OK"
    argv = calls(Path(os.environ["FAKE_LOG"]))[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "opus-x" and "cheap" not in argv
    (settings.bench_dir / "engine-policy.json").write_text("{broken")
    assert guard_main(["claude", "-p", "x"]) == REFUSED


def test_probe_records_each_engine(settings: Settings, fakes, tmp_path: Path, monkeypatch) -> None:
    for key, value in guard_env(settings, tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FAKE_CODEX", "limit")
    results = probe(settings)
    assert results["claude"]["ok"] and results["claude"]["status"] == "ok"
    assert not results["codex"]["ok"] and results["codex"]["status"] == "usage_limited"
    lines = summary(settings)
    assert lines[0].startswith("claude: ok") and lines[1].startswith("codex: paused until")  # at most 8 days
    assert lines[2].startswith("claude weekly window: 50% used")  # from the probe's rate_limit_event
    probes = [c["argv"] for c in calls(Path(os.environ["FAKE_LOG"]))]
    claude, codex = next(a for a in probes if "-p" in a), next(a for a in probes if a[:1] == ["exec"])
    assert json.loads(claude[claude.index("--mcp-config") + 1]) == {"mcpServers": {}} and "mcp_servers={}" in codex


def test_codex_lines_that_are_not_events_are_skipped() -> None:
    outcome = Codex().parse('[1, 2]\n"text"\n{"type": "thread.started", "thread_id": "t"}\n{"type": "turn.completed"}\n',
                            "", 0)
    assert outcome.status == "ok" and outcome.session_id == "t"


def test_an_interrupted_turn_stops_the_engine(fakes, tmp_path: Path, monkeypatch) -> None:
    import signal
    import time

    monkeypatch.setenv("FAKE_CLAUDE", "slow")

    def interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.alarm(2)
    try:
        with pytest.raises(KeyboardInterrupt):
            Claude().run(turn(tmp_path, new_session_id="s-3", timeout_s=60))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    pid = calls(Path(os.environ["FAKE_LOG"]))[-1]["pid"]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_kernels_meet_the_guards_first(settings: Settings, monkeypatch) -> None:
    from schub.bench.worker import _guarded_path

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    path = _guarded_path(settings).split(os.pathsep)
    guard = settings.bench_dir / "guard"
    assert path[0] == str(guard) and path[1:] == ["/usr/bin", "/bin"]
    assert os.access(guard / "claude", os.X_OK) and os.access(guard / "codex", os.X_OK)


def test_claude_reports_its_weekly_window(fakes, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_WEEK", "0.83")
    outcome = Claude().run(turn(tmp_path, new_session_id="s-4"))
    argv = calls(Path(os.environ["FAKE_LOG"]))[-1]["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert outcome.status == "ok" and outcome.details["rate_limit"]["unifiedWindows"]["seven_day"]["utilization"] == 0.83


def test_a_weekly_ceiling_is_a_policy_field() -> None:
    assert EnginePolicy().claude_weekly_ceiling == 0.8  # on by default: the student's own chats share the week
    assert EnginePolicy(claude_weekly_ceiling=0.8).claude_weekly_ceiling == 0.8
    for bad in (1.5, -0.1, "high"):
        with pytest.raises(PolicyError):
            EnginePolicy(claude_weekly_ceiling=bad)


def test_an_engine_that_answers_the_probe_is_no_longer_paused(settings: Settings, fakes, tmp_path: Path,
                                                               monkeypatch) -> None:
    for key, value in guard_env(settings, tmp_path).items():
        monkeypatch.setenv(key, value)
    cooldown = Cooldown(settings.bench_dir / "engine-cooldown.json")
    cooldown.mark("codex", "You hit your spend cap")
    assert not cooldown.ready("codex")
    probe(settings, engines=("codex",))  # the cap was raised: codex answers OK again
    assert cooldown.ready("codex")


def test_codex_keeps_its_sqlite_files_per_host(tmp_path: Path) -> None:
    import socket

    env = Codex().environment({"HOME": str(tmp_path), "PATH": "/usr/bin"})
    assert env["CODEX_SQLITE_HOME"] == str(tmp_path / ".codex-sqlite" / socket.gethostname().split(".")[0])
    assert Path(env["CODEX_SQLITE_HOME"]).is_dir() and env["PATH"] == "/usr/bin"
    assert Path(env["CODEX_SQLITE_HOME"]).stat().st_mode & 0o077 == 0
    assert Codex().environment({"HOME": str(tmp_path), "CODEX_SQLITE_HOME": "/set/by/owner"})["CODEX_SQLITE_HOME"] == \
        "/set/by/owner"
    assert Claude().environment(None) is None  # only Codex has the NFS problem



def test_only_real_actions_count_as_codex_work() -> None:
    """A notice ("error" item) before a failure is not work: a broken login must still pause the engine."""
    notice = "\n".join(json.dumps(e) for e in (
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"id": "i0", "type": "error", "message": "MCP server failed to start"}},
        {"type": "turn.failed", "error": {"message": "unauthorized"}}))
    assert Codex().parse(notice, "", 1).details["tool_calls"] == 0
    called = notice.replace('"type": "error", "message": "MCP server failed to start"', '"type": "mcp_tool_call"')
    assert Codex().parse(called, "", 1).details["tool_calls"] == 1


def test_the_probe_is_due_only_for_engines_in_use(settings: Settings) -> None:
    """After the owner narrowed the policy, an old record of the other engine must not make every watchdog run probe."""
    from schub.bench.engines import probe as probe_module

    fresh = datetime.now(timezone.utc).isoformat()
    settings.bench_dir.mkdir(parents=True, exist_ok=True)
    engine_policy.set_mode(settings.bench_dir / "engine-policy.json", "claude-only")
    (settings.bench_dir / "engine-probe.json").write_text(json.dumps({
        "claude": {"ok": True, "status": "ok", "at": fresh}, "codex": {"ok": True, "status": "ok", "at": "2026-01-01T00:00:00+00:00"}}))
    assert probe_module.due(settings) is False
    assert [line.split(":")[0] for line in summary(settings)] == ["claude"]
    engine_policy.set_mode(settings.bench_dir / "engine-policy.json", "mixed")
    assert probe_module.due(settings) is True


def test_a_turn_that_ran_past_its_deadline_keeps_the_evidence_of_its_work(fakes, tmp_path: Path, monkeypatch) -> None:
    """Killed at the slice's end after a tool call: it did work (counted), it did not fail to start."""
    monkeypatch.setenv("FAKE_CLAUDE", "toolhang")
    outcome = Claude().run(turn(tmp_path, session_id="s-1", timeout_s=2))
    assert outcome.status == "timed_out" and outcome.details["tool_calls"] == 1
    from schub.bench.goal_agent import did_work

    assert did_work(outcome)


def test_a_policy_file_from_before_the_ceiling_default_gets_it(tmp_path: Path) -> None:
    path = tmp_path / "engine-policy.json"
    path.write_text(json.dumps({"mode": "mixed", "claude_weekly_ceiling": 0.0}))  # written by an older sc-hub
    assert load(path).claude_weekly_ceiling == 0.8
    engine_policy.set_mode(path, "mixed", claude_weekly_ceiling=0.0)  # the owner's explicit choice now
    assert load(path).claude_weekly_ceiling == 0.0 and json.loads(path.read_text())["version"] == 3


def test_a_boolean_ceiling_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "engine-policy.json"
    path.write_text(json.dumps({"mode": "mixed", "claude_weekly_ceiling": False}))
    with pytest.raises(PolicyError):
        load(path)


def test_the_newest_model_at_xhigh_by_default_and_older_files_get_it(tmp_path: Path) -> None:
    policy = EnginePolicy()
    assert (policy.claude_model, policy.claude_effort, policy.codex_model, policy.codex_effort) == ("", "xhigh", "", "xhigh")
    path = tmp_path / "engine-policy.json"
    path.write_text(json.dumps({"mode": "mixed", "claude_effort": "", "codex_effort": "high", "version": 2}))
    older = load(path)
    assert older.claude_effort == "xhigh" and older.codex_effort == "high"  # an empty one was the old default
    engine_policy.set_mode(path, "mixed", claude_effort="")  # an explicit "": the CLI's own default, kept
    assert load(path).claude_effort == ""
