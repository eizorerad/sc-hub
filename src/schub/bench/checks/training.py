"""Checks for model training jobs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict

from ..models import CheckResult
from . import CheckDef, failed, passed


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


PROBE = ("import json, torch; c = torch.cuda.is_available(); print(json.dumps({'version': torch.__version__, "
         "'cuda': torch.version.cuda, 'count': torch.cuda.device_count() if c else 0, "
         "'name': torch.cuda.get_device_name(0) if c else ''}))")
CHECK_PYTHON = "SCHUB_CHECK_PYTHON"  # a --python job: probe the environment the job ran in, not sc-hub's


def _probe_other(python: str) -> CheckResult:
    try:
        done = subprocess.run([python, "-c", PROBE], capture_output=True, text=True, timeout=180, check=False)
        info = json.loads(done.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return failed("gpu_visible", f"torch could not be imported with {python}")
    if not info["count"]:
        return failed("gpu_visible", f"torch {info['version']} (CUDA {info['cuda']}) in {python} sees no GPU; the "
                      "node driver supports CUDA <= 12.8: install a cu118-cu128 build, or ask for --gpus 1",
                      cuda=info["cuda"])
    return passed("gpu_visible", f"{info['count']} GPU(s): {info['name']} ({python})", cuda=info["cuda"])


def check_gpu(path_of: Callable[[str], Path], p: NoParams) -> CheckResult:
    """torch sees a GPU. The default cu130 wheels import fine on this cluster but see none."""
    if os.environ.get(CHECK_PYTHON):
        return _probe_other(os.environ[CHECK_PYTHON])
    try:
        import torch
    except ImportError:
        return failed("gpu_visible", "torch is not installed in this environment")
    if not torch.cuda.is_available():
        build = getattr(torch.version, "cuda", None)
        return failed("gpu_visible", f"torch {torch.__version__} (CUDA {build}) sees no GPU; the node driver "
                      "supports CUDA <= 12.8: install a cu118-cu128 build, or ask for --gpus 1", cuda=build)
    return passed("gpu_visible", f"{torch.cuda.device_count()} GPU(s): {torch.cuda.get_device_name(0)}",
                  cuda=torch.version.cuda)


CHECKS = (
    CheckDef("gpu_visible", "torch sees a GPU (for training jobs)", NoParams, check_gpu, where="job"),
)
