"""The brick contract: typed params, a planning-time check, a state transform,
a resource estimate and a lazily imported implementation that runs in a job."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict

from ..config import Limits
from ..state import DatasetState, Frozen, Issue, error


class BrickParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BrickError(RuntimeError):
    """A data-level check failed inside the job; the message is shown to the user."""


class Resources(Frozen):
    cpus: int
    mem_gb: int
    time_min: int
    gpus: int = 0

    @property
    def gpu_hours(self) -> float:
        return self.gpus * self.time_min / 60


@dataclass(frozen=True)
class PlanContext:
    celltypist_dirs: tuple[Path, ...]
    limits: Limits
    env_id: str = ""
    code_ids: Mapping[str, str] = field(default_factory=dict)
    library_roots: tuple[Path, ...] = ()  # references and tools (kallisto index, Cell Ranger)
    # Profile + fingerprint of another dataset by name or path (merge_datasets); raises KeyError.
    dataset_lookup: Callable[[str], tuple[Any, str]] | None = None
    primary_path: str = ""  # the plan's own dataset (set by the planner)
    primary_fingerprint: str = ""

    def celltypist_models(self) -> tuple[str, ...]:
        found = {p.name for d in self.celltypist_dirs if d.is_dir() for p in d.glob("*.pkl")}
        return tuple(sorted(found))


@dataclass(frozen=True)
class StepIO:
    """Everything an implementation needs inside the Slurm job."""

    input: Path
    output: Path | None
    results_dir: Path
    state_in: DatasetState
    context: dict[str, str]


CheckFn = Callable[[DatasetState, Any, PlanContext], list[Issue]]
TransformFn = Callable[[DatasetState, Any], DatasetState]
ResourceFn = Callable[[DatasetState, Any], Resources]
# Identity of external inputs (a reference index, a tool version) folded into the step key.
KeyExtraFn = Callable[[DatasetState, Any, PlanContext], str]
ContextTransformFn = Callable[[DatasetState, Any, PlanContext], DatasetState]
# Datasets a step reads besides its input: {name: "<resolved path>\t<fingerprint>"}.
PinsFn = Callable[[DatasetState, Any, PlanContext], dict[str, str]]


@dataclass(frozen=True)
class BrickSpec:
    name: str
    version: str
    summary: str
    params_model: type[BrickParams]
    check: CheckFn
    transform: TransformFn
    resources: ResourceFn
    impl: str
    terminal: bool = False
    uses_gpu: bool = False
    source: bool = False  # reads FASTQ and writes the first count matrix
    key_extra: KeyExtraFn | None = None
    first_only: bool = False  # must be step 1 (it reads datasets, not a previous step)
    transform_ctx: ContextTransformFn | None = None  # replaces transform when it needs the context
    input_pins: PinsFn | None = None  # extra datasets, checked again at submit and in the job

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "terminal": self.terminal,
            "uses_gpu": self.uses_gpu,
            "reads_fastq": self.source,
            "params_schema": self.params_model.model_json_schema(),
        }


def scaled(state: DatasetState, base: float, per_100k: float) -> int:
    """Linear resource estimate in the number of cells."""
    return int(math.ceil(base + per_100k * state.n_obs / 100_000))


def need_raw_counts(state: DatasetState, brick: str, why: str) -> list[Issue]:
    if state.has_raw_counts():
        return []
    return [
        error(
            "needs_raw_counts",
            f"{brick} needs raw integer counts ({why}); X looks like '{state.x_kind}' and "
            "there is no layers['counts'].",
        )
    ]


def need_obs(state: DatasetState, column: str | None, role: str) -> list[Issue]:
    if column is None or state.has_obs(column):
        return []
    available = ", ".join(c.name for c in state.obs[:25]) or "none"
    return [
        error(
            "missing_obs",
            f"obs column '{column}' ({role}) not found; available: {available}",
        )
    ]
