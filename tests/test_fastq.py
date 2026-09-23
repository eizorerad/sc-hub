from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from schub.bricks import PlanContext
from schub.bricks.base import BrickError
from schub.bricks.cellranger_count import CellrangerParams
from schub.bricks.impl.cellranger_count import cellranger_command, stage_fastqs
from schub.bricks.impl.kb_count import _kb_command
from schub.datasets import dataset_fingerprint, list_datasets
from schub.fastq import FastqError, FastqSample, detect_samples, fastq_profile, load_manifest, write_manifest
from schub.library import find_dataset, find_tool, kallisto_ref
from schub.planner import StepRequest, build_plan
from schub.service import Hub, HubError
from schub.slurm import Slurm
from schub.stepfile import StepFile

QC = {"brick": "qc_filter", "params": {"min_genes": 10}}


def reads(folder: Path, sample: str = "pbmc", lanes: int = 2) -> list[str]:
    names = []
    for lane in range(1, lanes + 1):
        for read in ("R1", "R2", "I1"):
            path = folder / "fastqs" / f"{sample}_S1_L00{lane}_{read}_001.fastq.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{sample}{lane}{read}".encode())
            names.append(path.relative_to(folder).as_posix())
    return names


def fastq_dataset(folder: Path, **meta) -> Path:
    reads(folder)
    samples = detect_samples(folder)
    return write_manifest(folder, {"organism": "human", "technology": "10xv3", "samples": [s.model_dump() for s in samples], **meta})


def kallisto_index(library: Path, organism: str = "human") -> Path:
    folder = library / "refs" / "kallisto" / organism
    folder.mkdir(parents=True)
    (folder / "index.idx").write_bytes(b"idx")
    (folder / "t2g.txt").write_text("t\tg\tG\n")
    (folder / "ref.yaml").write_text("index_sha256: aaa\n")
    return folder


@pytest.fixture
def plan_ctx(settings, ctx) -> PlanContext:
    return PlanContext(celltypist_dirs=ctx.celltypist_dirs, limits=settings.limits, library_roots=settings.library_roots)


@pytest.fixture
def hub(settings, cluster, ctx) -> Hub:
    return Hub(settings, Slurm(cluster))


def test_detect_samples_pairs_lanes_and_skips_index_reads(tmp_path):
    reads(tmp_path, "pbmc")
    reads(tmp_path, "tumor-1", lanes=1)
    samples = detect_samples(tmp_path)
    assert [s.name for s in samples] == ["pbmc", "tumor-1"]
    assert samples[0].reads == (
        ("fastqs/pbmc_S1_L001_R1_001.fastq.gz", "fastqs/pbmc_S1_L001_R2_001.fastq.gz"),
        ("fastqs/pbmc_S1_L002_R1_001.fastq.gz", "fastqs/pbmc_S1_L002_R2_001.fastq.gz"),
    )
    with pytest.raises(FastqError, match="no paired 10x FASTQ"):
        detect_samples(tmp_path / "nothing")


def test_manifest_validation(tmp_path):
    path = fastq_dataset(tmp_path)
    manifest = load_manifest(path)
    assert manifest.technology == "10xv3" and len(manifest.read_files()) == 4
    with pytest.raises(ValueError, match="inside the dataset folder"):
        FastqSample(name="x", reads=(("../../etc/passwd.fastq.gz", "b.fastq.gz"),))
    with pytest.raises(ValueError, match="not supported"):
        write_manifest(tmp_path, {"technology": "smartseq", "samples": [manifest.samples[0].model_dump()]})
    (tmp_path / manifest.read_files()[0]).unlink()
    with pytest.raises(FastqError, match="missing read files"):
        load_manifest(path)


def test_profile_and_fingerprints(tmp_path):
    path = fastq_dataset(tmp_path, expected_cells=1200)
    state = fastq_profile(path).state
    assert state.source == "fastq" and state.samples == ("pbmc",) and state.n_obs == 1200 and state.fastq_gb == 0.0
    before = dataset_fingerprint(path)
    (tmp_path / load_manifest(path).read_files()[0]).write_bytes(b"changed reads")
    assert dataset_fingerprint(path) != before
    # Library copies with checksums: identity follows content, not location.
    meta = yaml.safe_load(path.read_text())
    meta["files"] = {f: {"sha256": "a" * 64, "size": (tmp_path / f).stat().st_size} for f in load_manifest(path).read_files()}
    path.write_text(yaml.safe_dump(meta))
    trusted = dataset_fingerprint(path, (tmp_path,))
    copy = tmp_path.parent / "copy"
    shutil.copytree(tmp_path, copy)
    assert dataset_fingerprint(copy / "fastq.yaml", (copy,)) == trusted != dataset_fingerprint(path)


def test_catalog_lists_fastq_datasets(settings):
    fastq_dataset(settings.library / "datasets" / "pbmc1k_v3_fastq", title="demo reads")
    entry = next(d for d in list_datasets(settings) if d.name == "pbmc1k_v3_fastq")
    assert entry.kind == "fastq" and entry.title == "demo reads" and entry.path.endswith("fastq.yaml")
    assert find_dataset(settings, "pbmc1k_v3_fastq").path == entry.path


def test_fastq_plans_need_a_counting_brick_first(settings, plan_ctx):
    profile = fastq_profile(fastq_dataset(settings.library / "datasets" / "reads"))
    codes = lambda plan: {i.code for i in plan.issues if i.level == "error"}  # noqa: E731
    assert "needs_counting" in codes(build_plan(profile, "fp", [StepRequest.model_validate(QC)], plan_ctx))
    assert "index_missing" in codes(build_plan(profile, "fp", [StepRequest(brick="kb_count")], plan_ctx))
    kallisto_index(settings.library)
    plan = build_plan(profile, "fp", [StepRequest(brick="kb_count"), StepRequest.model_validate(QC), StepRequest(brick="normalize_embed")], plan_ctx)
    assert plan.ok, plan.issues
    assert plan.final_state.x_kind == "normalized_log" or plan.final_state.has_raw_counts()
    assert plan.steps[1].state_in.mito_genes == 13 and plan.steps[1].state_in.has_obs("sample")


def test_counting_brick_on_a_count_matrix_is_refused(plan_ctx, write_h5ad):
    from schub.h5ad_profile import profile_h5ad

    from .conftest import make_adata

    profile = profile_h5ad(write_h5ad(make_adata()))
    plan = build_plan(profile, "fp", [StepRequest(brick="kb_count")], plan_ctx)
    assert [i.code for i in plan.issues if i.level == "error"] == ["not_fastq"]


def test_index_identity_is_part_of_the_step_key(settings, plan_ctx):
    profile = fastq_profile(fastq_dataset(settings.library / "datasets" / "reads"))
    ref = kallisto_index(settings.library)
    first = build_plan(profile, "fp", [StepRequest(brick="kb_count")], plan_ctx).steps[0].step_key
    (ref / "ref.yaml").write_text("index_sha256: bbb\n")
    assert build_plan(profile, "fp", [StepRequest(brick="kb_count")], plan_ctx).steps[0].step_key != first


def test_technology_must_be_known(settings, plan_ctx):
    folder = settings.library / "datasets" / "reads"
    reads(folder)
    path = write_manifest(folder, {"organism": "human", "samples": [s.model_dump() for s in detect_samples(folder)]})
    kallisto_index(settings.library)
    plan = build_plan(fastq_profile(path), "fp", [StepRequest(brick="kb_count")], plan_ctx)
    assert "no_technology" in {i.code for i in plan.issues}
    fixed = build_plan(fastq_profile(path), "fp", [StepRequest(brick="kb_count", params={"technology": "10xv2"})], plan_ctx)
    assert fixed.ok


def test_submit_passes_the_manifest_and_library_to_the_job(hub, settings, cluster):
    fastq_dataset(settings.library / "datasets" / "reads")
    kallisto_index(settings.library)
    plan = hub.plan("reads", [{"brick": "kb_count"}, QC])
    assert plan.ok, plan.issues
    manifest = hub.submit(plan.plan_id)
    step = StepFile.model_validate_json((Path(manifest.steps[0].step_dir) / "step.json").read_text())
    assert step.input.endswith("reads/fastq.yaml")
    assert str(settings.library) in step.context["library_roots"]


def test_register_fastq_describes_a_folder_in_data(hub, settings):
    reads(settings.data_dir / "my_run")
    entry = hub.register_fastq("my_run", "10xv3", "human", title="My run")
    assert entry.kind == "fastq" and entry.source == "private" and entry.name == "my_run"
    assert hub.inspect("my_run").state.samples == ("pbmc",)
    with pytest.raises(HubError, match="inside"):
        hub.register_fastq("../../etc", "10xv3", "human")
    with pytest.raises(HubError, match="not supported"):
        hub.register_fastq("my_run", "dropseq", "human")


def test_kb_command_uses_the_index_filter_and_all_reads(tmp_path):
    sample = FastqSample(name="s", reads=(("a_R1.fastq.gz", "a_R2.fastq.gz"), ("b_R1.fastq.gz", "b_R2.fastq.gz")))
    command = _kb_command(tmp_path, sample.reads, tmp_path / "ref", "10xv3", tmp_path / "out", True)
    assert command[1:3] == ["count", "-i"] and "10XV3" in command and "--gene-names" in command
    assert command[command.index("--filter") + 1] == "bustools"
    assert command[-4:] == [str(tmp_path / n) for n in ("a_R1.fastq.gz", "a_R2.fastq.gz", "b_R1.fastq.gz", "b_R2.fastq.gz")]
    assert "--filter" not in _kb_command(tmp_path, sample.reads, tmp_path, "10xv3", tmp_path, False)


def test_cellranger_needs_the_tool_and_a_reference(settings, plan_ctx):
    profile = fastq_profile(fastq_dataset(settings.library / "datasets" / "reads"))
    plan = build_plan(profile, "fp", [StepRequest(brick="cellranger_count")], plan_ctx)
    assert {"cellranger_missing", "reference_missing"} <= {i.code for i in plan.issues}
    version = settings.library / "tools" / "cellranger" / "cellranger-9.0.1"
    version.mkdir(parents=True)
    (version / "cellranger").write_text("#!/bin/sh\n")
    (version / "cellranger").chmod(0o755)
    (settings.library / "tools" / "cellranger" / "current").symlink_to("cellranger-9.0.1")
    ref = settings.library / "refs" / "cellranger" / "human"
    ref.mkdir(parents=True)
    (ref / "reference.json").write_text("{}")
    plan = build_plan(profile, "fp", [StepRequest(brick="cellranger_count")], plan_ctx)
    assert plan.ok, plan.issues
    assert plan.steps[0].resources.mem_gb == 64


def test_cellranger_stages_10x_names_and_builds_the_command(tmp_path):
    names = reads(tmp_path, "pbmc_1k_v3", lanes=2)
    sample = FastqSample(name="pbmc", reads=tuple((a, b) for a, b in zip(names[0::3], names[1::3])))
    prefix, folders = stage_fastqs(tmp_path, sample, tmp_path / "stage")
    assert prefix == "pbmc_1k_v3" and len(folders) == 1 and len(list(folders[0].iterdir())) == 4
    command = cellranger_command(Path("/t/cellranger"), Path("/ref"), sample, prefix, folders,
                                 CellrangerParams(expect_cells=3000), "16", 60)
    assert "--sample=pbmc_1k_v3" in command and "--create-bam=false" in command and "--expect-cells=3000" in command
    assert not any(c.startswith("--chemistry") for c in command)
    # The same file name in two flowcell folders: both are kept, in separate folders.
    for flowcell in ("fc1", "fc2"):
        for read in ("R1", "R2"):
            (tmp_path / flowcell).mkdir(exist_ok=True)
            (tmp_path / flowcell / f"s_S1_L001_{read}_001.fastq.gz").write_bytes(flowcell.encode())
    twin = FastqSample(name="s", reads=tuple((f"{fc}/s_S1_L001_R1_001.fastq.gz", f"{fc}/s_S1_L001_R2_001.fastq.gz") for fc in ("fc1", "fc2")))
    _, twin_folders = stage_fastqs(tmp_path, twin, tmp_path / "stage-twin")
    assert len(twin_folders) == 2
    assert any("," in c for c in cellranger_command(Path("/c"), Path("/r"), twin, "s", twin_folders, CellrangerParams(), "8", 30))
    bad = FastqSample(name="x", reads=(("odd_R1.fastq.gz", "odd_R2.fastq.gz"),))
    (tmp_path / "odd_R1.fastq.gz").write_bytes(b"")
    with pytest.raises(BrickError, match="10x FASTQ names"):
        stage_fastqs(tmp_path, bad, tmp_path / "stage2")


def test_find_tool_keeps_venv_symlinks_and_kallisto_lookup(tmp_path):
    base = tmp_path / "base-python"
    base.write_text("#!/bin/sh\n")
    base.chmod(0o755)
    version = tmp_path / "tools" / "cellxgene" / "1.3.0" / "bin"
    version.mkdir(parents=True)
    (version / "python").symlink_to(base)
    (tmp_path / "tools" / "cellxgene" / "current").symlink_to("1.3.0")
    found = find_tool((tmp_path,), "cellxgene", "bin/python")
    assert found == tmp_path / "tools" / "cellxgene" / "1.3.0" / "bin" / "python" and found.is_symlink()
    assert find_tool((tmp_path,), "r-seurat", "bin/Rscript") is None
    assert kallisto_ref((tmp_path,), "human") is None
    assert kallisto_ref((tmp_path, kallisto_index(tmp_path / "lib").parents[2]), "human") is not None


def test_chemistry_is_part_of_the_step_key(settings, plan_ctx):
    profile = fastq_profile(fastq_dataset(settings.library / "datasets" / "reads"))
    kallisto_index(settings.library)
    keys = {
        tech: build_plan(profile, "fp", [StepRequest(brick="kb_count", params={"technology": tech})], plan_ctx).steps[0].step_key
        for tech in ("10xv2", "10xv3")
    }
    assert keys["10xv2"] != keys["10xv3"]
