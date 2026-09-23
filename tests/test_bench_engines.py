"""Engine policy, pauses after usage limits, both adapters and the guard, on fake CLIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schub.bench.engines import policy as engine_policy
from schub.bench.engines.base import GUARD_DIR, McpServer, Turn
from schub.bench.engines.claude import Claude
from schub.bench.engines.codex import Codex
from schub.bench.engines.cooldown import Cooldown, is_limit, parse_reset
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
                 '{"grants": {"p": {"engines": ["gpt"]}}}', '{"claude_model": "x; rm -rf /"}'):
        path.write_text(text)
        with pytest.raises(PolicyError):
            load(path)


def test_the_owner_switches_and_grants(tmp_path: Path) -> None:
    path = tmp_path / "engine-policy.json"
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
    assert not is_limit("KeyError: 'gene'")
    dubai = parse_reset("You've hit your limit · resets 3pm (Asia/Dubai)", NOW)
    assert dubai == datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc)  # 15:00 +04
    assert parse_reset("resets at 7am", NOW) == datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc)  # past: tomorrow
    assert parse_reset("Try again at 2026-09-24T10:30:00Z.", NOW) == datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
    assert parse_reset("try again at 2020-01-01T00:00Z", NOW) is None  # an old date is not a reset
    assert parse_reset("the job finished at 3pm", NOW) is None


def test_a_paused_engine_is_ready_again_after_its_reset(tmp_path: Path) -> None:
    clock = {"now": NOW}
    cooldown = Cooldown(tmp_path / "cooldown.json", now=lambda: clock["now"])
    until = cooldown.mark("claude", "usage limit reached, resets at 10am")
    assert until == datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc) and not cooldown.ready("claude")
    assert cooldown.ready("codex")
    assert cooldown.mark("codex", "quota exceeded") == datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)
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
                                         ("garbage", "failed")])
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
    assert 'mcp_servers.schub.env={ SCHUB_LAB_AGENT_ENGINE = "x" }' in new
    assert 'mcp_servers.schub.default_tools_approval_mode="approve"' in new and new[-1] == "do the next step"
    monkeypatch.setenv("FAKE_CODEX", "limit")
    limited = Codex().run(turn(tmp_path, session_id=first.session_id))
    assert limited.status == "usage_limited" and "2099-01-01" in limited.error
    monkeypatch.setenv("FAKE_CODEX", "missing")
    assert Codex().run(turn(tmp_path, session_id="gone")).status == "session_missing"


def test_a_turn_past_its_time_is_stopped(fakes, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE", "slow")
    outcome = Claude().run(turn(tmp_path, new_session_id="s-2", timeout_s=1))
    assert outcome.status == "timed_out" and outcome.session_id == "s-2"


# ---- guard --------------------------------------------------------------------------


def test_pinned_replaces_the_callers_model() -> None:
    policy = EnginePolicy(claude_model="opus-x", claude_effort="high", codex_model="gpt-x")
    assert pinned("claude", ["-p", "hi", "--model", "cheap", "--effort=low"], policy) == \
        ["-p", "hi", "--model", "opus-x", "--effort", "high"]
    assert pinned("claude", ["-p", "hi"], EnginePolicy()) == ["-p", "hi"]
    assert pinned("codex", ["exec", "-m", "cheap", "-c", 'model="cheap"', "--json", "go"], policy) == \
        ["exec", "-c", 'model="gpt-x"', "--json", "go"]


def guard_env(settings: Settings, tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "SCHUB_ROOT": str(settings.root), "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "SCHUB_PYTHON": sys.executable}


def test_the_guard_refuses_what_the_policy_forbids(settings: Settings, fakes, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    engine_policy.set_mode(settings.bench_dir / "engine-policy.json", "claude-only", claude_model="opus-x")
    assert guard_main(["codex", "exec", "hi"]) == REFUSED
    done = subprocess.run([str(GUARD_DIR / "claude"), "-p", "Reply with exactly: OK", "--model", "cheap"],
                          env=guard_env(settings, tmp_path), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and json.loads(done.stdout)["result"] == "OK"
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
    assert lines[0].startswith("claude: ok") and lines[1].startswith("codex: paused until 2099-01-01T10:00")
