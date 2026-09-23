"""Sweeps (one parameter over several values as one experiment) and branch labels
(tags, pinned, archived) for students who run dozens of variants a day."""

from __future__ import annotations

from dataclasses import replace

import pytest

from schub.config import Limits
from schub.datasets import write_catalog_entry
from schub.projects import BranchSpec
from schub.queue import QueuedSubmission
from schub.runs import RunManifest
from schub.service import Hub, HubError
from schub.slurm import Slurm

from .conftest import library_datasets, make_adata

MAIN = BranchSpec(dataset="kang2018", steps=(
    {"brick": "qc_filter", "params": {"max_pct_mt": 100}}, {"brick": "normalize_embed"}))


@pytest.fixture
def hub(settings, cluster, ctx, write_h5ad):
    directory = library_datasets(settings) / "kang2018"
    write_catalog_entry(directory, {"title": "Kang"})
    write_h5ad(make_adata(), directory=directory)
    hub = Hub(replace(settings, limits=Limits(max_active_runs=2)), Slurm(cluster))
    hub.create_project("ifn", question="q")
    hub.save_branch("ifn", "main", MAIN)
    return hub


def test_a_sweep_makes_one_branch_per_value_and_remembers_what_varies(hub):
    result = hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, 1.0, 2.0], "res", "how many clusters?")
    assert result.branches == ("res-0-5", "res-1-0", "res-2-0")
    assert [p.ok for p in result.plans] == [True, True, True]
    spec = hub.projects.load_branch("ifn", "res-2-0")
    assert (spec.sweep, spec.sweep_step, spec.sweep_param, spec.sweep_value) == ("res", 2, "leiden_resolution", 2.0)
    assert spec.forked_from == "main@r1#2" and spec.reason == "how many clusters?"
    assert hub.sweep_members("ifn", "res") == ["res-0-5", "res-1-0", "res-2-0"]
    # the shared first step is one step for every variant
    keys = {hub.preview_branch("ifn", b).steps[0].step_key for b in result.branches}
    assert len(keys) == 1


def test_a_sweep_is_all_or_nothing(hub):
    hub.save_branch("ifn", "res-2-0", MAIN)
    with pytest.raises(HubError, match="res-2-0 already exists"):
        hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, 2.0], "res", "r")
    with pytest.raises(HubError, match="leiden_resolution"):
        hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, -1], "bad", "r")  # invalid value
    assert "res-0-5" not in hub.projects.branches("ifn") and "bad-0-5" not in hub.projects.branches("ifn")
    with pytest.raises(HubError, match="at least 2"):
        hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5], "one", "r")
    with pytest.raises(HubError, match="same branch name"):
        hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, "0.5"], "dup", "r")


def test_submitting_a_sweep_queues_what_is_above_the_cap(hub, cluster):
    hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, 1.0, 2.0], "res", "r")
    outcomes = hub.submit_sweep("ifn", "res")
    assert [type(o) for o in outcomes] == [RunManifest, RunManifest, QueuedSubmission]
    assert outcomes[2].branch == "res-2-0"


def test_branch_labels_do_not_make_revisions(hub):
    label = hub.label_branch("ifn", "main", add_tags=["baseline", "kang"], pinned=True)
    assert label.tags == ("baseline", "kang") and label.pinned and not label.archived
    label = hub.label_branch("ifn", "main", remove_tags=["kang"], archived=True)
    assert label.tags == ("baseline",) and label.archived
    assert hub.branch_labels("ifn")["main"] == label
    assert hub.projects.load_branch("ifn", "main").revision == 1
    with pytest.raises(HubError, match="no branch"):
        hub.label_branch("ifn", "nope", add_tags=["x"])
    with pytest.raises(HubError, match="tag"):
        hub.label_branch("ifn", "main", add_tags=["Bad Tag!"])


def test_cli_and_mcp_expose_sweeps_queue_and_labels(hub, settings, monkeypatch, capsys):
    import json as _json

    from schub import cli
    from schub.mcp_server import build_server

    monkeypatch.setattr(cli, "load_settings", lambda: hub.settings)
    monkeypatch.setattr(cli, "Hub", lambda s: hub)
    assert cli.main(["sweep", "ifn", "main", "2", "leiden_resolution", "[0.5, 1.0]", "--name", "res", "--reason", "r"]) == 0
    assert _json.loads(capsys.readouterr().out)["branches"] == ["res-0-5", "res-1-0"]
    assert cli.main(["branch-label", "ifn", "main", "--tag", "baseline", "--pin"]) == 0
    assert _json.loads(capsys.readouterr().out) == {"tags": ["baseline"], "pinned": True, "archived": False}
    assert cli.main(["queue"]) == 0 and _json.loads(capsys.readouterr().out) == {"waiting": [], "failed": []}
    hub.submit(hub.plan_branch("ifn", "res-0-5").plan_id)
    hub.submit(hub.plan_branch("ifn", "res-1-0").plan_id)
    hub.save_branch("ifn", "strict", BranchSpec(from_branch="main", overrides={"qc_filter": {"min_genes": 5}}))
    hub.submit(hub.plan_branch("ifn", "strict").plan_id)  # above the cap of 2: waits
    assert cli.main(["queue"]) == 0
    assert [w["branch"] for w in _json.loads(capsys.readouterr().out)["waiting"]] == ["strict"]  # JSON, not repr
    names = {t.name for t in build_server(hub)._tool_manager.list_tools()}
    assert {"sweep_branch", "submit_sweep", "queue_status", "cancel_queued", "label_branch"} <= names


def test_sweeps_queue_and_labels_through_the_mcp_server(hub):
    from schub.mcp_server import build_server

    from .test_mcp_server import call

    server = build_server(hub)
    sweep = call(server, "sweep_branch", {"project": "ifn", "branch": "main", "step": 2, "param": "leiden_resolution",
                                          "values": [0.5, 1.0, 2.0], "sweep": "res", "reason": "resolution"})
    assert not sweep.is_error and sweep.structured_content["branches"] == ["res-0-5", "res-1-0", "res-2-0"]
    submitted = call(server, "submit_sweep", {"project": "ifn", "sweep": "res"}).structured_content["result"]
    assert [s["status"] for s in submitted] == ["submitted", "submitted", "queued"]
    waiting = submitted[2]
    assert waiting["run_id"] is None and "no need to submit it again" in waiting["message"]
    status = call(server, "queue_status").structured_content
    assert [w["branch"] for w in status["waiting"]] == ["res-2-0"] and status["failed"] == []
    assert call(server, "cancel_queued", {"plan_id": waiting["plan_id"]}).structured_content["result"] is True
    label = call(server, "label_branch", {"project": "ifn", "branch": "res-0-5", "add_tags": ["sweep"], "archived": True})
    assert label.structured_content == {"tags": ["sweep"], "pinned": False, "archived": True}
    bad = call(server, "sweep_branch", {"project": "ifn", "branch": "main", "step": 2, "param": "leiden_resolution",
                                        "values": [0.5], "sweep": "one", "reason": "r"})
    assert bad.is_error and "at least 2" in bad.content[0].text


def test_a_broken_labels_file_is_reported_not_overwritten(hub):
    hub.label_branch("ifn", "main", add_tags=["baseline"])
    path = hub.projects.require("ifn") / "branch-labels.yaml"
    path.write_text(path.read_text() + "main: [unclosed\n")  # a hand edit gone wrong
    with pytest.raises(HubError, match="not valid YAML"):
        hub.label_branch("ifn", "main", pinned=True)
    assert "unclosed" in path.read_text()  # nothing was lost
    assert hub.branch_labels("ifn") == {}  # the dashboard still opens


def test_concurrent_labels_keep_every_change(hub):
    from concurrent.futures import ThreadPoolExecutor

    for n in range(6):
        hub.save_branch("ifn", f"b{n}", BranchSpec(from_branch="main"))
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda n: hub.label_branch("ifn", f"b{n}", add_tags=[f"t{n}"]), range(6)))
    assert {name: label.tags for name, label in hub.branch_labels("ifn").items()} == {f"b{n}": (f"t{n}",) for n in range(6)}


def test_revising_the_swept_step_keeps_the_sweep_fields_true(hub):
    hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, 2.0], "res", "r")
    hub.revise_branch("ifn", "res-0-5", 2, "finer", params={"leiden_resolution": 0.7})
    assert hub.projects.load_branch("ifn", "res-0-5").sweep_value == 0.7
    hub.revise_branch("ifn", "res-2-0", 1, "stricter QC", params={"min_genes": 5})  # another step
    assert hub.projects.load_branch("ifn", "res-2-0").sweep_value == 2.0


def test_resetting_the_swept_parameter_records_the_default(hub):
    hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, 2.0], "res", "r")
    hub.revise_branch("ifn", "res-2-0", 2, "back to the default", params={"leiden_resolution": None})
    assert hub.projects.load_branch("ifn", "res-2-0").sweep_value == 1.0
