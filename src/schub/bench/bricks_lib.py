"""sc-hub's bricks as functions a cell can call: `bench.run_brick("qc_filter", ...)`.

The same checks as a planned pipeline run first (a brick refuses data it cannot
handle), the implementation runs with the storage retries, and the brick's code
identity is recorded with the cell. GPU bricks refuse to run on a CPU-only kernel:
send such a cell with %%slurm --gpus 1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..bricks import BrickError, StepIO, get_brick
from ..config import load_settings
from ..execute import _run_with_retries, load_impl
from ..h5ad_profile import profile_h5ad
from ..library import celltypist_dirs
from ..provenance import code_id
from ..service import Hub
from . import ledger


def _gpu_visible() -> bool:
    try:
        import torch
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def run_brick(name: str, input: str | os.PathLike, output: str | os.PathLike | None, params: dict[str, Any],
              results_dir: str | os.PathLike | None) -> dict[str, Any]:
    try:
        spec = get_brick(name)
    except KeyError as exc:
        raise BrickError(str(exc)) from exc
    if spec.source:
        raise BrickError(f"{name} reads FASTQ folders; run it as a planned step (legacy tools) or in a job")
    try:
        p = spec.params_model.model_validate(params)
    except ValidationError as exc:
        raise BrickError(f"{name}: {exc.errors()[0]['loc']}: {exc.errors()[0]['msg']}") from exc
    source = Path(input).resolve()
    state = profile_h5ad(source).state
    settings = load_settings()
    issues = spec.check(state, p, Hub(settings).plan_context())
    errors = [i.message for i in issues if i.level == "error"]
    if errors:
        raise BrickError(f"{name} refuses this input: " + "; ".join(errors))
    for issue in issues:
        print(f"warning ({name}): {issue.message}")
    if spec.uses_gpu and not _gpu_visible():
        raise BrickError(f"{name} needs a GPU and this kernel has none: send the cell with %%slurm --gpus 1")
    work = Path(os.environ.get("SCHUB_PROJECT_DIR", source.parent)) / "work"
    target = None if spec.terminal else Path(output) if output else work / f"{source.stem}.{name}.h5ad"
    results = Path(results_dir) if results_dir else work / f"{source.stem}.{name}-results"
    results.mkdir(parents=True, exist_ok=True)
    io = StepIO(input=source, output=target, results_dir=results, state_in=state, context={
        "celltypist_dirs": os.pathsep.join(str(d) for d in celltypist_dirs(settings)),
        "library_roots": os.pathsep.join(str(r) for r in settings.library_roots),
    })
    implementation = load_impl(spec.impl)
    summary = _run_with_retries(lambda: implementation(io, p))
    ledger.record("note", brick=name, version=spec.version, code_id=code_id(spec))
    return {"summary": summary, "output": str(target) if target else None, "results_dir": str(results)}
