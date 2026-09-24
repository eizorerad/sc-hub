"""Reports: a spec assembled into a notebook from the journal, lenient checks, drafts and published folders."""

from __future__ import annotations

import json
from pathlib import Path

import nbformat
import pytest

from schub.bench.journal import Journal
from schub.bench.models import (Actor, CellEntry, CheckResult, Download, FileChange, JobRef, OutputItem)
from schub.bench.report_spec import Block, ReportSpec
from schub.bench.report_store import ReportStore, slug
from schub.bench.service import BenchError, BenchService
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WRITER = Actor(kind="lab_agent", client="claude-code", engine="claude", role="writer")


def cell(journal: Journal, why: str, code: str, **extra) -> str:
    cid = journal.allocate("c")
    journal.write_cell(CellEntry(ref=f"k562#{cid}", project="k562", cid=cid, why=why, expect="something", code=code,
                                 created=journal.now(), status=extra.pop("status", "ok"), **extra))
    return cid


@pytest.fixture
def study(settings: Settings) -> Journal:
    ProjectStore(settings).create("k562", question="Is the K562 screen usable?")
    journal = Journal(settings.projects_dir / "k562", "k562")
    cell(journal, "download the screen", "path = bench.fetch(URL)",
         outputs=(OutputItem(kind="stream", text="2057 perturbations, 310385 cells\n"),),
         downloads=(Download(url="https://plus.figshare.com/k562.h5ad", path="data/k562.h5ad", size=5_000,
                             sha256="ab" * 32),),
         files=(FileChange(path="results/table.csv", change="created"),))
    cell(journal, "knockdown per target", "table = knockdown(adata)\ntable.head()",
         outputs=(OutputItem(kind="result", text="median knockdown 0.83"),
                  OutputItem(kind="display", image="cells/c0002/fig-001.png")),
         jobs=(JobRef(job_id="206674", state="COMPLETED", exit_code=0),),
         check_results=(CheckResult(name="finite", status="fail", message="3 NaN"),), data_scope="twin")
    cell(journal, "rewrite the table", "table.to_csv('results/table.csv')",
         files=(FileChange(path="results/table.csv", change="modified"),))
    figure = journal.artifacts_dir("c0002") / "fig-001.png"
    figure.parent.mkdir(parents=True, exist_ok=True)
    figure.write_bytes(PNG)
    journal.add_note("finding", "Median knockdown is 0.83 over 2057 targets.", because=["k562#c0002"],
                     reverses_if="<img src=x onerror=alert(3)>")
    journal.add_note("verdict", "The screen is usable for the table.", because=["k562#c0002"], verdict="descriptive")
    journal.add_note("error", "I first read the raw counts as normalized.")
    journal.add_note("note", "Leo: check the guide column", audience="human")
    return journal


def service(settings: Settings, cluster: FakeCluster) -> BenchService:
    return BenchService(settings, Slurm(cluster))


def spec(**changes) -> ReportSpec:
    fields = {"title": "K562 screen: is it usable?", "summary": "Yes: knockdown is strong (0.83).",
              "blocks": (Block(text="## Data\n\nThe screen has 310385 cells. <script>alert(1)</script> "
                                    "[x](javascript:alert(2)), p < 0.05 and `x < 1`"),
                         Block(cell="c0001", show="outputs"),
                         Block(cell="k562#c0002"),
                         Block(figure="c0002", caption="Knockdown per target"),
                         Block(note="n0001"),
                         Block(text="About 42.5% of guides were weak.")),
              "left_out": {"n0003": "a slip fixed in the next cell"}}
    return ReportSpec(**{**fields, **changes})


def notebook(settings: Settings, folder: str) -> dict:
    return json.loads((settings.projects_dir / "k562" / folder / "report.ipynb").read_text())


def text_of(nb: dict) -> str:
    return "\n".join("".join(c["source"]) for c in nb["cells"])


def test_a_draft_is_assembled_from_the_journal(settings: Settings, cluster: FakeCluster, study: Journal) -> None:
    answer = service(settings, cluster).report("k562", spec(), actor=WRITER)
    assert answer.status == "draft" and answer.folder == "reports/draft" and answer.covers == "c0003"
    nb = notebook(settings, "reports/draft")
    nbformat.validate(nbformat.from_dict(nb))
    codes = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert "".join(codes[0]["source"]) == "path = bench.fetch(URL)"
    assert codes[0]["metadata"]["jupyter"] == {"source_hidden": True}  # show: outputs
    assert codes[1]["outputs"][0]["data"]["text/plain"] == ["median knockdown 0.83"]
    assert codes[1]["metadata"]["schub_ref"] == "k562#c0002"
    [figure] = [c for c in nb["cells"] if c.get("attachments")]
    assert "attachment:c0002-1.png" in "".join(figure["source"]) and figure["attachments"]["c0002-1.png"]["image/png"]
    text = text_of(nb)
    assert text.startswith("# K562 screen: is it usable?\n\n*k562* — Is the K562 screen usable?\n\nYes: knockdown")
    assert "job 206674 completed" in text and "check finite: fail" in text and "on a twin" in text
    assert "<script>alert(1)</script>" in text  # the notebook keeps the author's text; the HTML copy is sanitized
    assert "**Finding** (k562#n0001)" in text
    also = text[text.index("## Also in the journal"):]
    assert "n0002" in also and "n0003" in also and "a slip fixed in the next cell" in also
    assert "Leo: check the guide column" not in text  # a note for the student stays out
    assert "`https://plus.figshare.com/k562.h5ad` — sha256 abababababababab" in text
    warnings = " | ".join(answer.warnings)
    assert "42.5%" in warnings and "0.83" not in warnings and "310385" not in warnings  # shown numbers pass
    assert "c0002: check finite failed" in warnings and "c0002 ran on twins" in warnings
    assert "results/table.csv from c0001 changed later in c0003" in warnings
    assert "2 finding(s), verdict(s)" in warnings
    assert "## Notes on this report" in text and "42.5%" in text[text.index("## Notes on this report"):]


def test_the_html_copy_has_no_code(settings: Settings, cluster: FakeCluster, study: Journal) -> None:
    answer = service(settings, cluster).report("k562", spec())
    page = (settings.projects_dir / "k562" / answer.html).read_text()
    assert "median knockdown 0.83" in page and "table = knockdown(adata)" not in page
    assert "<script>alert(1)</script>" not in page and "javascript:alert" not in page
    assert "<img src=x onerror" not in page and "&lt;img src=x onerror" in page
    assert "p &lt; 0.05" in page and "<code>x &lt; 1</code>" in page
    assert "data:image/png;base64" in page  # the figure travels inside the page


def test_blocks_pointing_at_nothing_refuse_the_report(settings: Settings, cluster: FakeCluster, study: Journal) -> None:
    bad = spec(blocks=(Block(cell="c0099"), Block(figure="c0001"), Block(figure="c0002:3"), Block(note="n0004"),
                       Block(cell="other#c0001"), Block(note="c0001")))
    with pytest.raises(BenchError) as caught:
        service(settings, cluster).report("k562", bad)
    message = str(caught.value)
    for part in ("block 1: c0099 is not a cell", "block 2: c0001 has no figure", "block 3: c0002 has 1 figure(s)",
                 "block 4: n0004 is a note for the student only", "block 5: other#c0001 belongs to another project",
                 "block 6: 'c0001' is not a note id"):
        assert part in message
    assert not (settings.projects_dir / "k562" / "reports").exists()


def test_a_block_holds_exactly_one_thing() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        Block(text="x", cell="c0001")
    with pytest.raises(ValueError, match="exactly one of"):
        Block(caption="only a caption")


def test_publish_numbers_reports_and_keeps_them(settings: Settings, cluster: FakeCluster, study: Journal) -> None:
    bench = service(settings, cluster)
    bench.report("k562", spec())
    first = bench.report("k562", spec(), publish=True, actor=WRITER)
    assert first.status == "published" and first.folder == "reports/01-k562-screen-is-it-usable"
    reports = settings.projects_dir / "k562" / "reports"
    assert not (reports / "draft").exists()
    assert bench.report("k562", spec(), publish=True).status == "unchanged"  # no copy of the same report
    meta = json.loads((reports / "01-k562-screen-is-it-usable" / "report.json").read_text())
    assert meta["by"]["role"] == "writer" and meta["covers"] == "c0003" and meta["cited"][:2] == ["c0001", "c0002"]
    cell(study, "one more look", "print(1)", outputs=(OutputItem(kind="stream", text="1\n"),))
    second = bench.report("k562", spec(title="Второй отчёт"), publish=True)
    assert second.folder == "reports/02-report" and second.covers == "c0004"
    listed = bench.report("k562")
    assert listed.status == "listed" and [r.folder for r in listed.reports] == [first.folder, second.folder]
    assert listed.reports[0].html.endswith("report.html") and listed.reports[1].title == "Второй отчёт"
    index = (reports / "README.md").read_text()
    assert "[01-k562-screen-is-it-usable](01-k562-screen-is-it-usable/report.ipynb)" in index and "c0004" in index
    assert ReportStore(settings.projects_dir / "k562").latest().folder == second.folder


def test_a_short_report_warns_about_the_summary(settings: Settings, cluster: FakeCluster, study: Journal) -> None:
    answer = service(settings, cluster).report("k562", ReportSpec(title="t", blocks=(Block(text="Nothing yet."),),
                                                                  left_out={"n0042": "?"}))
    joined = " | ".join(answer.warnings)
    assert "no summary" in joined and "left_out names n0042" in joined


def test_without_nbconvert_the_notebook_is_still_written(settings: Settings, cluster: FakeCluster, study: Journal,
                                                         monkeypatch) -> None:
    monkeypatch.setattr("schub.bench.report_store.to_html", lambda notebook: None)
    answer = service(settings, cluster).report("k562", spec())
    assert answer.html == "" and any("no HTML copy" in w for w in answer.warnings)
    assert (settings.projects_dir / "k562" / answer.notebook).is_file()


def test_the_writer_cannot_reopen_the_work(settings: Settings, cluster: FakeCluster, study: Journal) -> None:
    with pytest.raises(BenchError, match="does not change the hand-over"):
        service(settings, cluster).handoff("k562", "all done", "active", next_action="more", actor=WRITER)


def test_slugs() -> None:
    assert slug("K562 screen: is it usable?") == "k562-screen-is-it-usable"
    assert slug("Отчёт") == "report" and len(slug("x" * 99)) == 40


def test_numbers_are_claimed_and_repeats_are_confirmed(settings: Settings, cluster: FakeCluster,
                                                       study: Journal) -> None:
    bench = service(settings, cluster)
    reports = settings.projects_dir / "k562" / "reports"
    (reports / ".numbers").mkdir(parents=True)
    (reports / ".numbers" / "1").write_text("")  # another publish holds number 1 right now
    first = bench.report("k562", spec(), publish=True)
    assert first.folder == "reports/02-k562-screen-is-it-usable"
    assert oct((settings.projects_dir / "k562" / first.notebook).stat().st_mode & 0o777) == "0o444"
    store = ReportStore(settings.projects_dir / "k562")
    later = "9999-01-01T00:00:00.000+00:00"
    assert store.published_since(later) is None
    bench.now = lambda: later
    assert bench.report("k562", spec(), publish=True).status == "unchanged"
    assert store.published_since(later).folder == first.folder  # the writer's repeat counts as published


def test_a_draft_replaces_the_last_one_and_blocks_can_repeat(settings: Settings, cluster: FakeCluster,
                                                            study: Journal) -> None:
    bench = service(settings, cluster)
    bench.report("k562", spec())
    twice = spec(blocks=(Block(cell="c0001"), Block(cell="c0001"), Block(note="n0001"), Block(note="n0001")))
    bench.report("k562", twice)
    nb = notebook(settings, "reports/draft")
    ids = [c["id"] for c in nb["cells"]]
    assert len(ids) == len(set(ids)) and "About 42.5%" not in text_of(nb)
    assert not [p for p in (settings.projects_dir / "k562" / "reports").iterdir() if p.name.startswith(".")]
