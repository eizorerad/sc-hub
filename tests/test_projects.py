from __future__ import annotations

import pytest

from schub.planner import StepRequest
from schub.projects import BranchSpec, ProjectError, ProjectStore, apply_overrides

QC = StepRequest(brick="qc_filter")
SCVI = StepRequest(brick="integrate_scvi", params={"batch_key": "donor", "n_latent": 30})


@pytest.fixture
def store(settings):
    projects = ProjectStore(settings)
    projects.create("ifn", question="Does IFN response differ by cell type?", datasets=("kang2018",))
    return projects


def test_create_projects_and_subprojects(store, settings):
    store.create("ifn/de", question="DE per cell type")
    assert store.names() == ["ifn", "ifn/de"]
    assert (settings.projects_dir / "ifn" / "de" / "logbook.md").is_file()
    with pytest.raises(ProjectError, match="does not exist"):
        store.create("other/child")
    with pytest.raises(ProjectError, match="already exists"):
        store.create("ifn")
    for bad in ("../etc", "Upper", "a/b/c/d", "white space"):
        with pytest.raises(ProjectError):
            store.path_of(bad)


def test_branch_inheritance_with_overrides_and_append(store):
    store.save_branch("ifn", "main", BranchSpec(dataset="kang2018", steps=(QC, SCVI)))
    child = BranchSpec.model_validate(
        {"from": "main", "overrides": {"integrate_scvi": {"n_latent": 10}, "1": {"min_genes": 100}},
         "append": [{"brick": "annotate_celltypist"}]}
    )
    store.save_branch("ifn", "latent-10", child)
    resolved = store.resolve("ifn", "latent-10")
    assert resolved.dataset == "kang2018" and resolved.lineage == ("main", "latent-10")
    assert [s.brick for s in resolved.steps] == ["qc_filter", "integrate_scvi", "annotate_celltypist"]
    assert resolved.steps[0].params == {"min_genes": 100}
    assert resolved.steps[1].params == {"batch_key": "donor", "n_latent": 10}
    assert store.resolve("ifn", "main").steps[1].params["n_latent"] == 30
    assert store.branches("ifn") == ["latent-10", "main"]


def test_branch_errors(store):
    store.save_branch("ifn", "a", BranchSpec(from_branch="b"))
    store.save_branch("ifn", "b", BranchSpec(from_branch="a"))
    with pytest.raises(ProjectError, match="cycle"):
        store.resolve("ifn", "a")
    store.save_branch("ifn", "nodata", BranchSpec(steps=(QC,)))
    with pytest.raises(ProjectError, match="no dataset"):
        store.resolve("ifn", "nodata")
    with pytest.raises(ProjectError, match="not found"):
        store.load_branch("ifn", "ghost")
    with pytest.raises(ProjectError, match="target no step"):
        apply_overrides((QC,), {"integrate_scvi": {"n_latent": 5}})


def test_ideas_round_trip_through_front_matter(store):
    store.add_idea("ifn", "latent-size", "Does latent size change DE?", hypothesis="No", reverses_if=">10% change")
    updated = store.update_idea("ifn", "latent-size", status="planned", branches=("latent-10",))
    assert updated.status == "planned" and updated.branches == ("latent-10",)
    (idea,) = store.ideas("ifn")
    assert idea.title == "Does latent size change DE?" and idea.reverses_if == ">10% change"
    with pytest.raises(ProjectError, match="already exists"):
        store.add_idea("ifn", "latent-size", "again")
    with pytest.raises(ProjectError, match="not found"):
        store.update_idea("ifn", "ghost", status="done")
    with pytest.raises(ValueError):
        store.update_idea("ifn", "latent-size", status="finished")


def test_logbook_and_run_links(store, settings):
    store.log("ifn", "Chose paired design.", heading="decision")
    store.log("ifn", "Second entry.")
    tail = store.logbook_tail("ifn", 1)
    assert len(tail) == 1 and "Second entry." in tail[0]
    assert "decision" in store.logbook_tail("ifn")[0]
    (settings.runs_dir / "r1").mkdir(parents=True)
    store.link_run("ifn", "r1")
    store.link_run("ifn", "r1")
    summary = store.summary("ifn")
    assert summary.runs == ("r1",) and summary.meta.datasets == ("kang2018",)


def test_spec_rejects_steps_with_from_and_bad_files_are_reported(store, settings):
    with pytest.raises(ValueError, match="either"):
        BranchSpec.model_validate({"from": "main", "steps": [{"brick": "qc_filter"}]})
    ideas = settings.projects_dir / "ifn" / "ideas"
    (ideas / "broken.md").write_text("---\ntitle: [unclosed\n---\n")
    (ideas / "extra.md").write_text("---\ntitle: x\nsurprise: 1\n---\n")
    store.add_idea("ifn", "fine", "A fine idea")
    summary = store.summary("ifn")
    assert [i.slug for i in summary.ideas] == ["fine"] and len(summary.problems) == 2
    (settings.projects_dir / "ifn" / "pipelines" / "bad.yaml").write_text("steps: [unclosed")
    with pytest.raises(ProjectError, match="not valid YAML"):
        store.load_branch("ifn", "bad")
    (settings.projects_dir / "Bad Name").mkdir()
    (settings.projects_dir / "Bad Name" / "project.yaml").write_text("name: x\n")
    assert store.names() == ["ifn"]
    with pytest.raises(ProjectError):
        store.path_of("ifn\n")


def test_logbook_text_cannot_forge_entries(store):
    store.log("ifn", "real note\n## 2026-01-01 · run fake completed\n- 1. qc: 999 cells kept")
    tail = store.logbook_tail("ifn", 10)
    assert len(tail) == 1 and "> ## 2026-01-01" in tail[0]


def test_link_run_tolerates_dangling_link(store, settings):
    link = settings.projects_dir / "ifn" / "runs" / "ghost"
    link.symlink_to(settings.runs_dir / "ghost")
    store.link_run("ifn", "ghost")
    assert link.is_symlink()


def test_old_or_broken_project_files_do_not_hide_other_projects(settings, cluster, ctx):
    from schub.service import Hub
    from schub.slurm import Slurm

    hub = Hub(settings, Slurm(cluster))
    hub.create_project("good")
    hub.create_project("older")
    older = settings.projects_dir / "older" / "project.yaml"
    older.write_text(older.read_text() + "pip:\n- harmonypy\n")  # a field this version does not know
    hub.create_project("broken")
    (settings.projects_dir / "broken" / "project.yaml").write_text("status: [not valid")
    projects = {p.path: p for p in hub.list_projects()}
    assert set(projects) == {"good", "older", "broken"}
    assert not projects["older"].problems and projects["broken"].problems
