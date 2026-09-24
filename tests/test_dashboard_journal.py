from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from schub.bench.journal import Journal
from schub.bench.models import Actor, CellEntry, CheckResult, Download, JobRef, OutputItem
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
                            "jobs": (JobRef(job_id="812", state="COMPLETED", exit_code=0),),
                            "downloads": (Download(url="https://github.com/lab/model.git", path="work/repos/model",
                                                   size=0, sha256="", commit="0123456789abcdef0123"),
                                          Download(url="https://zenodo.org/f.h5ad", path="data/f.h5ad", size=5,
                                                   sha256="fedcba9876543210"))}),
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
    """The index and every project's page (loaded on a click), as one text."""
    info = build_dashboard(Hub(settings, Slurm(cluster)))
    return Path(info.path).read_text() + "".join(page_of(settings, p.stem.replace(".", "/"))
                                                 for p in sorted((settings.view_dir / "jproj").glob("*.js")))


def page_of(settings: Settings, project: str) -> str:
    text = (settings.view_dir / "jproj" / f"{project.replace('/', '.')}.js").read_text()
    prefix = f"window.SCHUB_JPAGE=window.SCHUB_JPAGE||{{}};window.SCHUB_JPAGE[{json.dumps(project)}]="
    assert text.startswith(prefix)
    return json.loads(text[len(prefix):].rstrip().rstrip(";"))


def test_the_journal_tab_shows_the_work(settings: Settings, cluster: FakeCluster) -> None:
    bench_project(settings)
    page = build(settings, cluster)
    assert re.findall(r'data-tab="([a-z]+)"', page) == ["journal"]  # no brick-era data: no brick-era tabs
    section = page_of(settings, "ifn")
    assert section.index("load &lt;b&gt;Kang") < section.index("plot QC") < section.index("train")  # the story's order
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "check table_columns" in page and "lacks pvalue" in page and "job 812 completed" in page
    assert "Leo: please check the donor column" in page  # the student sees notes meant for them
    assert "cloned <code>https://github.com/lab/model.git</code> <span class='muted small'>commit 0123456789ab" in page
    assert "downloaded <code>https://zenodo.org/f.h5ad</code> <span class='muted small'>sha256 fedcba987654" in page
    assert "its cells turn out to be doublets" in page  # the decisions table behind the menu
    assert "codex-mcp-client" in page
    assert (settings.view_dir / "jfig" / "ifn" / "c0002" / "fig-001.png").read_bytes() == PNG
    assert 'src="jfig/ifn/c0002/fig-001.png"' in page
    assert "ifn: check table_columns is failing (its latest result)" in page


def test_a_check_fixed_later_raises_no_alert(settings: Settings, cluster: FakeCluster) -> None:
    journal = bench_project(settings)
    cid = journal.allocate("c")
    journal.write_cell(CellEntry(ref=f"ifn#{cid}", project="ifn", cid=cid, why="fixed", expect="pass", code="x",
                                 created=journal.now(), status="ok",
                                 check_results=(CheckResult(name="table_columns", status="pass", message="ok"),)))
    assert "is failing" not in build(settings, cluster)


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


def test_the_brick_era_tabs_are_gone_even_with_the_legacy_flag(settings: Settings, cluster: FakeCluster) -> None:
    page = build(dataclasses.replace(settings, legacy_tools=True), cluster)
    assert re.findall(r'data-tab="([a-z]+)"', page) == ["journal"]
    assert "No bench work yet" in page


def test_a_published_report_is_one_line_on_the_journal_page(settings: Settings, cluster: FakeCluster) -> None:
    from schub.bench.report_spec import Block, ReportSpec
    from schub.bench.service import BenchService

    journal = bench_project(settings)
    bench = BenchService(settings, Slurm(cluster))
    spec = ReportSpec(title="IFN answer", summary="24673 cells.", blocks=(Block(cell="c0001", show="outputs"),))
    published = bench.report("ifn", spec, publish=True)
    cid = journal.allocate("c")
    journal.write_cell(CellEntry(ref=f"ifn#{cid}", project="ifn", cid=cid, why="later", expect="x", code="x",
                                 created=journal.now(), status="ok"))
    build(settings, cluster)
    section = page_of(settings, "ifn")
    assert '<a href="jrep/ifn/01-ifn-answer/report.html" target="_blank"' in section
    assert "1 newer cell since the report" in section and "Outcome<span class=\"muted\"> from the report" in section
    assert '<p class="jp-outcome-text">24673 cells.</p>' in section
    assert 'data-jnb="report:ifn:reports/01-ifn-answer"' in section and 'data-name="ifn.01-ifn-answer"' in section
    folder = settings.view_dir / "jrep" / "ifn" / "01-ifn-answer"
    assert (folder / "report.html").read_bytes() == (settings.projects_dir / "ifn" / published.html).read_bytes()
    script = (folder / "report.js").read_text()
    assert script.startswith("window.SCHUB_JNB=") and '"report:ifn:reports/01-ifn-answer"' in script
    stale = settings.view_dir / "jrep" / "gone" / "old.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("x")
    build(settings, cluster)
    assert not stale.exists() and (folder / "report.js").exists()


def test_the_navigator_groups_projects_and_nests_variants(settings: Settings, cluster: FakeCluster) -> None:
    import shutil
    from datetime import datetime, timedelta, timezone

    from schub.bench.checkpoint import CheckpointStore
    from schub.dashboard.views_journal import page_version

    def made(name: str, disposition: str, days_ago: float = 0.0) -> None:
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="milliseconds")
        ProjectStore(settings).create(name, question=f"Does {name} replicate?")
        journal = Journal(settings.projects_dir / name, name, now=lambda: when)
        cid = journal.allocate("c")
        journal.write_cell(CellEntry(ref=f"{name}#{cid}", project=name, cid=cid, why=f"first look at {name}",
                                     expect="x", code="x", created=when, status="ok"))
        if disposition != "active":
            CheckpointStore(settings.projects_dir / name, now=lambda: when).write(disposition, reason="r")

    made("alpha", "active")
    made("alpha/strict", "blocked")
    made("alpha/strict/deep", "complete")
    made("beta", "complete", days_ago=1)
    made("gamma", "active", days_ago=10)
    made("delta", "complete")
    (settings.bench_dir / "evals").mkdir(parents=True)
    (settings.bench_dir / "evals" / "runs.jsonl").write_text('{"project": "delta"}\n')
    build(settings, cluster)
    index = (settings.view_dir / "index.html").read_text()
    nav = index[index.index('class="jnav"'):index.index('id="jpage"')]
    assert re.findall(r'class="jgroup" data-group="([a-z]+)"', nav) == ["blocked", "done", "idle", "eval"]
    blocked = nav[nav.index('data-group="blocked"'):nav.index('data-group="done"')]
    assert 'data-path="alpha"' in blocked and '<details class="jkids" open><summary>2 variants' in blocked
    assert blocked.index('data-path="alpha/strict"') < blocked.index('data-path="alpha/strict/deep"')
    assert "first look at" not in index  # pages load on a click: the index carries only the navigator
    alpha = page_of(settings, "alpha")
    assert '<a class="jrow" href="#journal/alpha/strict">' in alpha and "Variants" in alpha
    deep = page_of(settings, "alpha/strict/deep")
    assert '<a href="#journal/alpha">alpha</a> / <a href="#journal/alpha/strict">strict</a> / deep' in deep
    version = re.search(r'data-path="beta" data-v="([0-9a-f]+)"', index)[1]
    assert version == page_version(page_of(settings, "beta"))
    shutil.rmtree(settings.projects_dir / "beta")
    build(settings, cluster)
    assert not (settings.view_dir / "jproj" / "beta.js").exists() and (settings.view_dir / "jproj" / "alpha.js").exists()


def test_the_outcome_is_plain_text() -> None:
    from schub.dashboard.collect_journal import _lead

    text = "# Title\n**Question.** Does GEARS (*Nature Biotechnology* 2023) hold? 2*3, `R`\n\n## Next\n- more"
    assert _lead(text) == "Question. Does GEARS (Nature Biotechnology 2023) hold? 2*3, R\n\nNext\n- more"
    assert _lead("a\n\n" + "b" * 900).endswith(" …") and len(_lead("x" * 3000, 2000)) < 2010
    assert _lead("__init__ and #3 donors, p < 0.05*") == "__init__ and #3 donors, p < 0.05*"
    from schub.dashboard.collect_journal import _group
    from datetime import datetime, timezone
    assert _group("active", "2026-09-01T10:00:00", False, datetime(2026, 9, 24, tzinfo=timezone.utc)) == "idle"
    assert _group("active", "", False, datetime(2026, 9, 24, tzinfo=timezone.utc)) == "working"


def test_text_reaches_the_lazy_page_escaped(settings: Settings, cluster: FakeCluster) -> None:
    from schub.bench.checkpoint import CheckpointStore

    journal = bench_project(settings)
    bad = "<img src=x onerror=alert(1)>"
    CheckpointStore(settings.projects_dir / "ifn").write_handoff(f"Where we are {bad}")
    journal.add_note("finding", f"found {bad}", because=["ifn#c0001"])
    cid = journal.allocate("c")
    journal.write_cell(CellEntry(ref=f"ifn#{cid}", project="ifn", cid=cid, why=f"why {bad}", expect=bad, code=bad,
                                 created=journal.now(), status="ok"))
    build(settings, cluster)
    page = page_of(settings, "ifn")
    assert bad not in page and page.count("&lt;img src=x onerror=alert(1)&gt;") >= 5


def test_a_live_variant_lifts_an_evaluation_project(settings: Settings, cluster: FakeCluster) -> None:
    from schub.bench.checkpoint import CheckpointStore

    for name in ("ev-run", "ev-run/retry"):
        ProjectStore(settings).create(name, question="q")
        Journal(settings.projects_dir / name, name).add_note("note", "started")
    CheckpointStore(settings.projects_dir / "ev-run/retry").write("blocked", reason="needs you")
    (settings.bench_dir / "evals").mkdir(parents=True)
    (settings.bench_dir / "evals" / "runs.jsonl").write_text('{"project": "ev-run"}\n')
    build(settings, cluster)
    index = (settings.view_dir / "index.html").read_text()
    assert re.findall(r'class="jgroup" data-group="([a-z]+)"', index) == ["blocked"]
