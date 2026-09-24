"""The breadth evaluation: the request file, launching runs as lab-agent goals, scoring a journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from schub.bench.checkpoint import CheckpointStore
from schub.bench.evals import EvalError, EvalRequest, launch, load_requests, markdown, matrix, runs_path, score
from schub.bench.goal import Goal
from schub.bench.journal import Journal
from schub.bench.models import CellEntry, CheckResult, Download, JobRef
from schub.config import Settings
from schub.slurm import Slurm
from tests.conftest import FakeCluster

REQUESTS = Path(__file__).parents[1] / "evals" / "requests.yaml"


def test_the_request_file_is_valid_and_broad() -> None:
    requests = load_requests(REQUESTS)
    assert 15 <= len(requests) <= 20
    assert {"k562-table-qc", "paper-gears"} <= {r.id for r in requests}
    assert {r.source for r in requests} >= {"acceptance", "vcc-style", "course-style"}


def test_bad_request_files_are_refused(tmp_path: Path) -> None:
    for text in ("requests: []", "requests:\n  - {id: Bad Id, source: x, prompt: 'long enough prompt here!'}",
                 "requests:\n  - {id: a, source: x, prompt: 'long enough prompt here!'}\n"
                 "  - {id: a, source: x, prompt: 'long enough prompt here!'}"):
        path = tmp_path / "r.yaml"
        path.write_text(text)
        with pytest.raises(EvalError):
            load_requests(path)


def test_each_run_is_a_goal_with_its_engine(settings: Settings, cluster: FakeCluster) -> None:
    request = EvalRequest(id="kang-pseudobulk-de", source="course-style", prompt="Test DE in Kang 2018 monocytes.",
                          max_turns=4)
    runs = launch(settings, Slurm(cluster), [request], ["claude", "codex"], max_turns=3, gpu_minutes=20)
    assert [r["engine"] for r in runs] == ["claude", "codex"] and len(cluster.jobs) == 2
    for run in runs:
        config = Goal(settings, run["project"]).config()
        assert config.engine == run["engine"] and config.max_turns == 3 and "hand over" in config.objective
        assert "at most 20 GPU-minutes and 5 GB of downloads" in config.objective
        assert config.report is False  # a report would add turns the other column never had
        assert len(run["project"]) <= 41
    assert len(runs_path(settings).read_text().splitlines()) == 2
    with pytest.raises(EvalError):
        launch(settings, Slurm(cluster), [request], ["gpt"])


def test_a_run_is_scored_from_its_journal(settings: Settings, cluster: FakeCluster) -> None:
    request = EvalRequest(id="k562-table-qc", source="acceptance", prompt="Build the K562 table and QC it.",
                          expect={"downloads": 1, "slurm_jobs": 1, "checks": ["perturbation"],
                                  "notes": ["finding"], "files": ["work/*.csv"]})
    [run] = launch(settings, Slurm(cluster), [request], ["claude"])
    project, folder = run["project"], settings.projects_dir / run["project"]
    journal = Journal(folder, project)
    for status, checks in (("pass", "fail"), ("pass", "pass")):
        cid = journal.allocate("c")
        journal.write_cell(CellEntry(
            ref=f"{project}#{cid}", project=project, cid=cid, why="w", expect="e", code="x", created=journal.now(),
            status="ok", downloads=(Download(url="https://z/f", path="data/f", size=1, sha256="ab"),),
            jobs=(JobRef(job_id="9", state="COMPLETED"),),
            check_results=(CheckResult(name="perturbation", status=checks, message="m"),)))
    (folder / "work" / "table.csv").write_text("a\n1\n")
    Goal(settings, project).event("turn_finished", engine="claude", status="ok", cost_usd=0.5)
    Goal(settings, project).event("turn_finished", engine="claude", status="ok", cost_usd=2.0, role="writer")
    first = score(settings, project, request)
    assert first["report_turns"] == 1 and first["turns"] == 1 and first["cost_usd"] == 0.5
    assert not first["complete"] and first["missing"] == ["notes"] and first["checks_failing"] == []
    journal.add_note("finding", "done", because=[f"{project}#c0002"])
    journal.add_note("incident", "The kernel stopped: nothing ran for 10 minutes, so the workbench stopped.")
    CheckpointStore(folder).write("complete")
    [row] = matrix(settings, [request])
    assert row["complete"] and row["expectations_met"] == 5 and row["turns"] == 1 and row["cost_usd"] == 0.5
    table = markdown([row])
    assert "| k562-table-qc | claude | yes | 5/5 | - | - | 1 | 0.5 |" in table
