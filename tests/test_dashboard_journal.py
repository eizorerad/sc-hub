from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from schub.bench.journal import Journal
from schub.bench.models import Actor, CellEntry, CheckResult, JobRef, OutputItem
from schub.config import Settings
from schub.dashboard import build_dashboard
from schub.projects import ProjectStore
from schub.service import Hub
from schub.slurm import Slurm
from tests.conftest import FakeCluster

PNG = b"\x89PNG\r\n\x1a\nfigure"


def bench_project(settings: Settings) -> Journal:
    ProjectStore(settings).create("ifn", question="How do PBMCs answer IFN-beta?")
    journal = Journal(settings.projects_dir / "ifn", "ifn")
    codex = Actor(kind="chat", client="codex-mcp-client")
    for text, status, extra in (
        ("load <b>Kang</b>", "ok", {"outputs": (OutputItem(kind="stream", text="24673 cells\n<script>alert(1)</script>"),)}),
        ("plot QC", "ok", {"outputs": (OutputItem(kind="display", image="cells/c0002/fig-001.png"),),
                           "check_results": (CheckResult(name="table_columns", status="fail", message="lacks pvalue"),)}),
        ("train", "error", {"outputs": (OutputItem(kind="error", ename="KeyError", text="KeyError: 'gene'"),),
                            "jobs": (JobRef(job_id="812", state="COMPLETED", exit_code=0),)}),
    ):
        cid = journal.allocate("c")
        journal.write_cell(CellEntry(ref=f"ifn#{cid}", project="ifn", cid=cid, why=text, expect="something",
                                     code=f"# {text}", created=journal.now(), status=status, actor=codex, **extra))
    figure = journal.artifacts_dir("c0002") / "fig-001.png"
    figure.parent.mkdir(parents=True, exist_ok=True)
    figure.write_bytes(PNG)
    journal.add_note("decision", "keep the megakaryocyte cluster", because=["ifn#c0001"],
                     reverses_if="its cells turn out to be doublets")
    journal.add_note("note", "Leo: please check the donor column", audience="human")
    return journal


def build(settings: Settings, cluster: FakeCluster):
    info = build_dashboard(Hub(settings, Slurm(cluster)))
    return Path(info.path).read_text()


def test_the_journal_tab_shows_the_work(settings: Settings, cluster: FakeCluster) -> None:
    bench_project(settings)
    page = build(settings, cluster)
    assert re.findall(r'data-tab="([a-z]+)"', page) == ["journal"]  # no brick-era data: no brick-era tabs
    section = page[page.index('data-journal="ifn"'):]
    assert section.index("train") < section.index("plot QC") < section.index("load &lt;b&gt;Kang")  # newest first
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "check table_columns" in page and "lacks pvalue" in page and "job 812 completed" in page
    assert "Leo: please check the donor column" in page  # the student sees notes meant for them
    assert "its cells turn out to be doublets" in page  # the decisions table behind the menu
    assert "codex-mcp-client" in page
    assert (settings.view_dir / "jfig" / "ifn" / "c0002" / "fig-001.png").read_bytes() == PNG
    assert 'src="jfig/ifn/c0002/fig-001.png"' in page
    assert "ifn: 1 cell(s) with failed checks" in page


def test_the_notebook_is_valid_and_kept_in_the_journal(settings: Settings, cluster: FakeCluster) -> None:
    bench_project(settings)
    build(settings, cluster)
    notebook = json.loads((settings.view_dir / "jnb" / "ifn.ipynb").read_text())
    assert (notebook["nbformat"], notebook["nbformat_minor"]) == (4, 5)
    ids = [c["id"] for c in notebook["cells"]]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"[a-zA-Z0-9-_]{1,64}", i) for i in ids)
    code = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    assert [c["metadata"]["schub_ref"] for c in code] == ["ifn#c0001", "ifn#c0002", "ifn#c0003"]
    assert code[1]["outputs"][0]["data"]["image/png"]  # the figure travels with the notebook
    assert code[2]["outputs"][0]["output_type"] == "error"
    assert (settings.projects_dir / "ifn" / "journal" / "notebook.ipynb").exists()
    script = (settings.view_dir / "jnb" / "ifn.js").read_text()
    assert script.startswith('window.SCHUB_JNB=window.SCHUB_JNB||{};window.SCHUB_JNB["ifn"]=')


def test_alerts_for_a_stopped_bench(settings: Settings, cluster: FakeCluster) -> None:
    bench_project(settings)
    settings.bench_dir.mkdir(parents=True, exist_ok=True)
    (settings.bench_dir / "STOP").write_text("")
    assert "The bench is stopped" in build(settings, cluster)


def test_legacy_tabs_with_the_flag(settings: Settings, cluster: FakeCluster) -> None:
    page = build(dataclasses.replace(settings, legacy_tools=True), cluster)
    assert re.findall(r'data-tab="([a-z]+)"', page) == ["journal", "projects", "pipelines", "experiments"]
    assert "No bench work yet" in page
