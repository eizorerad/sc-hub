"""Checks for model training jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict

from ..models import CheckResult
from . import CheckDef, failed, passed


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def check_gpu(path_of: Callable[[str], Path], p: NoParams) -> CheckResult:
    """torch sees a GPU. The default cu130 wheels import fine on this cluster but see none."""
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
