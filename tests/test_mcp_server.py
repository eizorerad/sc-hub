from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

from schub.bricks import REGISTRY
from schub.datasets import write_catalog_entry
from schub.mcp_server import build_server
from schub.service import Hub
from schub.slurm import Slurm

from .conftest import library_datasets, make_adata

EXPECTED_TOOLS = {
    "list_datasets",
    "inspect_dataset",
    "list_bricks",
    "describe_brick",
    "plan_pipeline",
    "submit_plan",
    "run_status",
    "list_runs",
    "run_logs",
    "run_results",
    "cancel_run",
    "make_notebook",
    "cluster_status",
    "list_projects",
    "create_project",
    "save_branch",
    "plan_branch",
    "add_idea",
    "update_idea",
    "add_logbook_entry",
    "fetch_asset",
    "make_dashboard",
    "list_recipes",
    "recipe_steps",
    "register_fastq",
    "import_seurat",
    "start_session",
    "list_sessions",
    "stop_session",
}


@pytest.fixture
def server(settings, cluster, ctx, write_h5ad):
    directory = library_datasets(settings) / "pbmc3k"
    write_catalog_entry(directory, {"title": "PBMC"})
    write_h5ad(make_adata(), directory=directory)
    return build_server(Hub(settings, Slurm(cluster)))


def call(server, tool, args=None):
    async def _run():
        async with Client(server) as client:
            return await client.call_tool(tool, args or {})

    return anyio.run(_run)


def test_tools_are_listed(server):
    async def _run():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert anyio.run(_run) == EXPECTED_TOOLS


def test_plan_and_submit_through_mcp(server, cluster, settings):
    plan = call(server, "plan_pipeline", {"dataset": "pbmc3k", "steps": [{"brick": "qc_filter"}]})
    assert not plan.is_error
    summary = plan.structured_content
    assert summary["ok"] is True and summary["steps"][0]["brick"] == "qc_filter"
    run = call(server, "submit_plan", {"plan_id": summary["plan_id"]})
    assert run.structured_content["plan_id"] == summary["plan_id"]
    assert len(cluster.jobs) == 1
    log_lines = next(settings.logs_dir.glob("calls-*.jsonl")).read_text().splitlines()
    assert [json.loads(line)["tool"] for line in log_lines] == ["plan_pipeline", "submit_plan"]


def test_errors_become_tool_errors_and_are_audited(server, settings):
    result = call(server, "inspect_dataset", {"dataset": "/etc/passwd"})
    assert result.is_error
    record = json.loads(next(settings.logs_dir.glob("calls-*.jsonl")).read_text().splitlines()[-1])
    assert record["ok"] is False and record["tool"] == "inspect_dataset"


def test_discovery_tools(server):
    datasets = call(server, "list_datasets").structured_content
    assert datasets["result"][0]["name"] == "pbmc3k"
    bricks = call(server, "list_bricks").structured_content["result"]
    assert len(bricks) == len(REGISTRY)
    assert call(server, "describe_brick", {"name": "qc_filter"}).structured_content["name"] == "qc_filter"
    assert call(server, "cluster_status").structured_content["partitions"][0]["name"] == "ws-ia"
    assert call(server, "inspect_dataset", {"dataset": "pbmc3k"}).structured_content["state"]["n_obs"] == 60


def test_project_tools(server, cluster):
    created = call(server, "create_project", {"project": "pbmc", "question": "Which clusters?"})
    assert not created.is_error
    saved = call(server, "save_branch", {
        "project": "pbmc", "name": "main", "dataset": "pbmc3k", "steps": [{"brick": "qc_filter"}],
    }).structured_content
    assert saved["ok"] and saved["branch"] == "main"
    child = call(server, "save_branch", {
        "project": "pbmc", "name": "strict", "from_branch": "main",
        "overrides": {"qc_filter": {"min_genes": 500}},
    }).structured_content
    assert child["steps"][0]["params"]["min_genes"] == 500
    idea = call(server, "add_idea", {"project": "pbmc", "slug": "strict-qc", "title": "Stricter QC?"})
    assert idea.structured_content["status"] == "open"
    assert call(server, "update_idea", {"project": "pbmc", "slug": "strict-qc", "status": "planned"}).structured_content["status"] == "planned"
    assert call(server, "add_logbook_entry", {"project": "pbmc", "text": "note"}).structured_content["result"]
    projects = call(server, "list_projects").structured_content["result"]
    assert projects[0]["branches"] == ["main", "strict"]
    assert call(server, "save_branch", {"project": "nope", "name": "x"}).is_error
    dash = call(server, "make_dashboard").structured_content
    assert dash["projects"] == 1
