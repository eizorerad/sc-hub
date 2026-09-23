"""`%%slurm`: run this cell as a Slurm job instead of in the kernel.

    %%slurm --gpus 1 --cpus 8 --mem 64G --time 6h [--partition gpu] [--bash] [--python PATH]
    <self-contained code: it reads and writes files, not kernel variables>

The kernel never waits for the job. The cell's journal entry gets the job; when the
job ends its state, files and checks are added to the same entry.
"""

from __future__ import annotations

import os

from IPython.core.magic import Magics, cell_magic, magics_class

from ..config import load_settings
from ..slurm import Slurm
from . import kernel_api, ledger
from .slurm_cell import parse_line, submit_cell


@magics_class
class BenchMagics(Magics):
    @cell_magic
    def slurm(self, line: str, cell: str) -> None:
        settings = load_settings()
        spec = parse_line(line, settings.partition)
        ref = kernel_api.current_cell()
        project = os.environ.get("SCHUB_PROJECT") or ref.partition("#")[0]
        submitted = submit_cell(settings, Slurm(), project, kernel_api.project_dir(), ref, cell, spec,
                                kernel_api.current_checks())
        job = submitted.job
        ledger.record("job", **job.model_dump())
        gpu = f", {spec.gpus} GPU" if spec.gpus else ""
        print(f"Submitted Slurm job {job.job_id} ({spec.partition}, {spec.cpus} CPU, {spec.mem_gb} GB{gpu}, "
              f"{spec.minutes} min).\nThe journal entry of this cell gets its state, files and checks when it "
              f"ends; its log: {job.log}")
        for warning in submitted.warnings:
            print(f"warning: {warning}")


def load_ipython_extension(ipython) -> None:  # noqa: ANN001 - IPython's own type
    ipython.register_magics(BenchMagics)
