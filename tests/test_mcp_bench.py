from __future__ import annotations

import dataclasses
import json
import threading

import anyio
import pytest
from mcp import Client
from mcp.types import Implementation

from schub.bench import clients
from schub.bench.clients import ClientProfile
from schub.bench.config import BenchConfig
from schub.bench.inbox import Inbox
from schub.bench.runner import Runner
from schub.config import Settings
from schub.mcp_server import build_server
from schub.service import Hub
from schub.slurm import Slurm
from tests.conftest import FakeCluster, library_datasets, make_adata
from schub.datasets import write_catalog_entry

CODEX = Implementation(name="codex-mcp-client", version="0.150.0")


@pytest.fixture
def bench(settings: Settings, monkeypatch) -> Settings:
    fast = dataclasses.replace(settings, bench=BenchConfig(poll_s=0.05))
    for name in list(clients.PROFILES):
        monkeypatch.setitem(clients.PROFILES, name,
                            clients.PROFILES[name].model_copy(update={"run_wait_s": 0.2}))
    return fast


@pytest.fixture
def server(bench: Settings, cluster: FakeCluster):
    return build_server(Hub(bench, Slurm(cluster)))


def call(server, tool: str, args: dict | None = None, client_info: Implementation | None = CODEX):
    async def _run():
        async with Client(server, client_info=client_info) as client:
            return await client.call_tool(tool, args or {})

    return anyio.run(_run)


def ok(result) -> dict:
    assert not result.is_error, result.content[0].text
    return result.structured_content


def test_a_first_session(server, bench: Settings, cluster: FakeCluster) -> None:
    assert ok(call(server, "projects"))["result"] == []
    ok(call(server, "create_project", {"project": "ifn", "question": "How do PBMCs answer IFN-beta?"}))
    result = ok(call(server, "run", {"project": "ifn", "code": "print(1)", "why": "smoke", "expect": "1"}))
    assert result["ref"] == "ifn#c0001" and result["status"] == "queued" and "Slots:" in result["workbench"]
    assert [r.actor.client for r in Inbox(bench.bench_dir).pending()] == ["codex-mcp-client"]
    assert "schub-bench-workbench" in cluster.names.values()
    card = ok(call(server, "projects"))["result"][0]
    assert card["project"] == "ifn" and card["cells"] == 1
    audit = [json.loads(line) for line in next(bench.logs_dir.glob("calls-*.jsonl")).read_text().splitlines()]
    assert [a["tool"] for a in audit] == ["projects", "create_project", "run", "projects"]
    assert audit[2]["args"]["client"] == "codex-mcp-client"


def test_why_and_expect_are_required(server) -> None:
    ok(call(server, "create_project", {"project": "p", "question": "q"}))
    result = call(server, "run", {"project": "p", "code": "1", "why": " ", "expect": "e"})
    assert result.is_error and "why" in result.content[0].text


def test_notes_handoff_and_the_journal(server, bench: Settings) -> None:
    ok(call(server, "create_project", {"project": "p", "question": "q"}))
    ref = ok(call(server, "run", {"project": "p", "code": "1", "why": "w", "expect": "e"}))["ref"]
    bad = call(server, "note", {"project": "p", "kind": "decision", "text": "use K562", "because": [ref]})
    assert bad.is_error and "reverses_if" in bad.content[0].text
    ok(call(server, "note", {"project": "p", "kind": "registration", "text": "DE needs 2 donors per arm"}))
    ok(call(server, "note", {"project": "p", "kind": "note", "text": "Leo: cancel job 5 tonight", "audience": "human"}))
    checkpoint = ok(call(server, "handoff", {"project": "p", "text": "# State\n- QC pending", "disposition": "active",
                                             "next_action": "run QC on the twin"}))
    assert checkpoint["actor"]["client"] == "codex-mcp-client"
    view = ok(call(server, "journal", {"project": "p"}))
    assert view["handoff"].startswith("# State") and view["checkpoint"]["next_action"] == "run QC on the twin"
    texts = [e.get("text", "") for e in view["entries"]]
    assert "Leo: cancel job 5 tonight" not in json.dumps(view) and any("not shown" in t for t in texts)
    newer = ok(call(server, "journal", {"project": "p", "since": view["newest"]}))
    assert newer["entries"] == []


def test_files_never_show_tokens(server, bench: Settings) -> None:
    ok(call(server, "create_project", {"project": "p", "question": "q"}))
    secret = bench.root / "sessions" / "123"
    secret.mkdir(parents=True)
    (secret / "connection.json").write_text('{"path": "/lab?token=SEKRET"}')
    (bench.projects_dir / "p" / "work" / "notes.txt").write_text("hello")
    for path in ("../../sessions/123/connection.json", str(secret / "connection.json"), "/etc/passwd"):
        result = call(server, "files", {"project": "p", "path": path})
        assert result.is_error and "SEKRET" not in result.content[0].text
    listing = ok(call(server, "files", {"project": "p", "path": "work"}))
    assert [e["name"] for e in listing["entries"]] == ["notes.txt"]
    assert ok(call(server, "files", {"project": "p", "path": "work/notes.txt"}))["text"] == "hello"


def test_files_profile_h5ad_in_the_library(server, bench: Settings, write_h5ad) -> None:
    directory = library_datasets(bench) / "pbmc3k"
    write_catalog_entry(directory, {"title": "PBMC"})
    path = write_h5ad(make_adata(), directory=directory)
    answer = ok(call(server, "files", {"path": str(path)}))
    assert answer["kind"] == "h5ad" and answer["profile"]["state"]["n_obs"] == 60
    assert ok(call(server, "datasets"))["datasets"][0]["name"] == "pbmc3k"
    assert ok(call(server, "datasets", {"name": "pbmc3k"}))["profile"]["state"]["x_kind"] == "raw_counts"


def test_skills_and_stop(server, bench: Settings, cluster: FakeCluster) -> None:
    names = [s["name"] for s in ok(call(server, "skills"))["skills"]]
    assert {"resume", "rigor", "mbzuai_slurm"} <= set(names)
    assert "hand-over" in ok(call(server, "skills", {"name": "resume"}))["text"]
    assert call(server, "skills", {"name": "../x"}).is_error
    ok(call(server, "create_project", {"project": "p", "question": "q"}))
    ref = ok(call(server, "run", {"project": "p", "code": "import time; time.sleep(5)", "why": "w", "expect": "e"}))["ref"]
    assert "interrupt" in call(server, "stop", {"target": ref}).content[0].text
    job = next(j for j, n in cluster.names.items() if n == "schub-bench-workbench")
    call(server, "stop", {"target": "workbench"})
    assert cluster.jobs[job] == "CANCELLED"
    assert call(server, "stop", {"target": "everything"}).is_error


def test_profiles_follow_the_client_name() -> None:
    assert clients.detect("codex-mcp-client") == "codex"
    assert clients.detect("claude-code") == "claude-code"
    assert clients.detect("claude-ai") == "claude-desktop"
    assert clients.detect("openai-mcp") == "chatgpt"
    assert clients.detect("") == "unknown"
    assert clients.profile_for("whatever", env={"SCHUB_CLIENT_PROFILE": "codex"}).name == "codex"
    assert isinstance(clients.profile_for("claude-code", elicitation=True), ClientProfile)


@pytest.mark.kernel
def test_end_to_end_through_mcp(bench: Settings, cluster: FakeCluster, monkeypatch) -> None:
    monkeypatch.setitem(clients.PROFILES, "codex", clients.PROFILES["codex"].model_copy(update={"run_wait_s": 30}))
    server = build_server(Hub(bench, Slurm(cluster)))
    ok(call(server, "create_project", {"project": "p", "question": "q"}))
    runner = Runner(bench, job_id="8000", slurm=Slurm(runner=cluster))
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    try:
        result = ok(call(server, "run", {"project": "p", "code": "print(6 * 7)", "why": "w", "expect": "42"}))
        assert result["status"] == "ok" and result["outputs"][0]["text"] == "42\n"
        entry = ok(call(server, "journal", {"project": "p"}))["entries"][-1]
        assert entry["by"] == "codex-mcp-client" and entry["status"] == "ok"
    finally:
        (bench.bench_dir / "STOP").write_text("")
        thread.join(timeout=60)


def test_figures_reach_clients_that_show_images(server, bench: Settings) -> None:
    from schub.bench.journal import Journal
    from schub.bench.models import CellEntry, OutputItem

    ok(call(server, "create_project", {"project": "p", "question": "q"}))
    ref = ok(call(server, "run", {"project": "p", "code": "plt.show()", "why": "w", "expect": "a plot"}))["ref"]
    journal = Journal(bench.projects_dir / "p", "p")
    figure = journal.artifacts_dir("c0001") / "fig-001.png"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    journal.write_cell(CellEntry(ref=ref, project="p", cid="c0001", why="w", expect="a plot", code="plt.show()",
                                 created=journal.now(), status="ok",
                                 outputs=(OutputItem(kind="display", image="cells/c0001/fig-001.png"),)))
    claude = call(server, "wait", {"ref": ref}, client_info=Implementation(name="claude-code", version="2.1"))
    assert [c.type for c in claude.content] == ["text", "image"] and claude.structured_content["status"] == "ok"
    codex = call(server, "wait", {"ref": ref})
    assert [c.type for c in codex.content] == ["text"]


def test_run_takes_checks_and_the_checks_skill_lists_them(server, bench: Settings) -> None:
    ok(call(server, "create_project", {"project": "p", "question": "q"}))
    ok(call(server, "run", {"project": "p", "code": "1", "why": "w", "expect": "e",
                            "checks": [{"name": "file", "params": {"path": "work/x.csv"}}]}))
    [request] = Inbox(bench.bench_dir).pending()
    assert request.checks[0].name == "file"
    text = ok(call(server, "skills", {"name": "checks"}))["text"]
    assert "`perturbation(" in text and "`de_design(" in text
    assert "checks" in [s["name"] for s in ok(call(server, "skills"))["skills"]]
