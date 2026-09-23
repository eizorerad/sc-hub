from __future__ import annotations

from pathlib import Path

import pytest

from schub.bench.bricks_lib import run_brick
from schub.bricks import BrickError
from schub.config import Settings
from tests.conftest import make_adata


@pytest.fixture
def data(settings: Settings, tmp_path: Path, monkeypatch) -> dict[str, Path]:
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.setenv("SCHUB_PROJECT_DIR", str(tmp_path / "project"))
    counts, lognorm = tmp_path / "counts.h5ad", tmp_path / "lognorm.h5ad"
    make_adata().write_h5ad(counts)
    make_adata(x="lognorm").write_h5ad(lognorm)
    return {"counts": counts, "lognorm": lognorm}


def test_unknown_bricks_and_bad_params_are_refused(data: dict[str, Path]) -> None:
    with pytest.raises(BrickError):
        run_brick("no_such_brick", data["counts"], None, {}, None)
    with pytest.raises(BrickError, match="qc_filter"):
        run_brick("qc_filter", data["counts"], None, {"min_genes": -5}, None)


def test_the_bricks_own_checks_refuse_wrong_data(data: dict[str, Path]) -> None:
    with pytest.raises(BrickError, match="refuses"):
        run_brick("pseudobulk_de", data["lognorm"], None, {
            "condition_key": "label", "reference": "ctrl", "treatment": "stim", "replicate_key": "donor"}, None)


def test_gpu_bricks_ask_for_a_gpu_job(data: dict[str, Path]) -> None:
    with pytest.raises(BrickError, match="%%slurm --gpus 1"):
        run_brick("integrate_scvi", data["counts"], None, {"batch_key": "donor"}, None)


def test_fastq_bricks_are_not_cell_functions(data: dict[str, Path]) -> None:
    with pytest.raises(BrickError, match="FASTQ"):
        run_brick("kb_count", data["counts"], None, {}, None)
