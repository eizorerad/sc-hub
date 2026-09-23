"""Turn a list of brick requests into a validated, content-addressed plan.

Planning is pure: it reads a dataset profile and never touches Slurm. Every
step gets a key derived from its input key, brick version and params, so an
unchanged prefix of a pipeline is reused instead of recomputed.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from pydantic import Field, ValidationError

from .bricks import REGISTRY, BrickSpec, PlanContext, Resources
from .bricks.base import BrickParams
from .config import Limits
from .h5ad_profile import DatasetProfile
from .hashing import stable_hash
from .state import DatasetState, Frozen, GeneIds, Issue, Species, error, warning


class StepRequest(Frozen):
    brick: str
    params: dict[str, Any] = Field(default_factory=dict)


class DatasetOverrides(Frozen):
    species: Species | None = None
    gene_ids: GeneIds | None = None


class PlannedStep(Frozen):
    index: int
    brick: str
    version: str
    params: dict[str, Any]
    resources: Resources
    step_key: str
    code_id: str
    terminal: bool
    state_in: DatasetState


class Plan(Frozen):
    plan_id: str
    dataset: str
    dataset_fingerprint: str
    steps: tuple[PlannedStep, ...]
    issues: tuple[Issue, ...]
    gpu_hours: float
    final_state: DatasetState
    project: str | None = None
    branch: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not any(i.level == "error" for i in self.issues)

    def summary(self) -> PlanSummary:
        return PlanSummary(
            plan_id=self.plan_id,
            ok=self.ok,
            dataset=self.dataset,
            issues=self.issues,
            steps=tuple(
                StepSummary(index=s.index, brick=s.brick, params=s.params, resources=s.resources)
                for s in self.steps
            ),
            gpu_hours=self.gpu_hours,
            final_obs_columns=tuple(c.name for c in self.final_state.obs),
            project=self.project,
            branch=self.branch,
        )


class StepSummary(Frozen):
    index: int
    brick: str
    params: dict[str, Any]
    resources: Resources


class PlanSummary(Frozen):
    plan_id: str
    ok: bool
    dataset: str
    issues: tuple[Issue, ...]
    steps: tuple[StepSummary, ...]
    gpu_hours: float
    final_obs_columns: tuple[str, ...]
    project: str | None = None
    branch: str | None = None


def _apply_overrides(state: DatasetState, overrides: DatasetOverrides | None) -> DatasetState:
    if overrides is None:
        return state
    changes = {k: v for k, v in overrides.model_dump().items() if v is not None}
    return state.update(**changes)


def _validate_params(spec: BrickSpec, raw: Mapping[str, Any], step: int) -> tuple[BrickParams | None, list[Issue]]:
    try:
        return spec.params_model.model_validate(dict(raw)), []
    except ValidationError as exc:
        issues = [
            error("bad_params", f"{spec.name}.{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}", step)
            for e in exc.errors()
        ]
        return None, issues


def _clamp(res: Resources, limits: Limits, step: int) -> tuple[Resources, list[Issue]]:
    clamped = Resources(
        cpus=min(res.cpus, limits.max_cpus),
        mem_gb=min(res.mem_gb, limits.max_mem_gb),
        time_min=min(res.time_min, limits.max_time_min),
        gpus=res.gpus,
    )
    if clamped == res:
        return res, []
    return clamped, [
        warning("resources_clamped", f"Estimate {res.model_dump()} capped to partition limits.", step)
    ]


def _source_issues(state: DatasetState, spec: BrickSpec, step: int) -> list[Issue]:
    """FASTQ must be counted first, and only FASTQ can be counted."""
    if state.source == "fastq" and not spec.source:
        return [error("needs_counting", f"The dataset is FASTQ reads; start with kb_count or cellranger_count, not {spec.name}.", step)]
    if spec.source and state.source != "fastq":
        return [error("not_fastq", f"{spec.name} counts FASTQ reads; this dataset is already a count matrix.", step)]
    return []


def build_plan(
    profile: DatasetProfile,
    fingerprint: str,
    requests: Sequence[StepRequest],
    ctx: PlanContext,
    overrides: DatasetOverrides | None = None,
    registry: Mapping[str, BrickSpec] = REGISTRY,
) -> Plan:
    state = _apply_overrides(profile.state, overrides)
    issues: list[Issue] = []
    steps: list[PlannedStep] = []
    prev_key = fingerprint
    if not requests:
        issues.append(error("empty_plan", "A plan needs at least one step."))
    if len(requests) > ctx.limits.max_steps:
        issues.append(error("too_many_steps", f"At most {ctx.limits.max_steps} steps per plan."))
    for index, request in enumerate(requests, start=1):
        spec = registry.get(request.brick)
        if spec is None:
            issues.append(
                error("unknown_brick", f"'{request.brick}'; available: {', '.join(registry)}", index)
            )
            continue
        params, param_issues = _validate_params(spec, request.params, index)
        issues += param_issues
        if params is None:
            continue
        if steps and steps[-1].terminal:
            issues.append(
                error("after_terminal", f"'{steps[-1].brick}' only writes tables; it must be last.", index)
            )
        source_issues = _source_issues(state, spec, index)
        issues += source_issues
        if not source_issues:
            issues += [i.model_copy(update={"step": index}) for i in spec.check(state, params, ctx)]
        resources, clamp_issues = _clamp(spec.resources(state, params), ctx.limits, index)
        issues += clamp_issues
        dumped = params.model_dump(mode="json")
        code = ctx.code_ids.get(spec.name, "")
        extra = spec.key_extra(state, params, ctx) if spec.key_extra else ""
        parts = (prev_key, spec.name, spec.version, dumped, code, ctx.env_id) + ((extra,) if extra else ())
        key = stable_hash(*parts)
        steps.append(
            PlannedStep(
                index=index,
                brick=spec.name,
                version=spec.version,
                params=dumped,
                resources=resources,
                step_key=key,
                code_id=code,
                terminal=spec.terminal,
                state_in=state,
            )
        )
        state = spec.transform(state, params)
        prev_key = key
    gpu_hours = round(sum(s.resources.gpu_hours for s in steps), 2)
    if gpu_hours > ctx.limits.max_gpu_hours_per_plan:
        issues.append(
            error(
                "gpu_budget",
                f"Plan asks for {gpu_hours} GPU-hours; limit is {ctx.limits.max_gpu_hours_per_plan}.",
            )
        )
    return Plan(
        plan_id=stable_hash(fingerprint, [s.step_key for s in steps], length=12),
        dataset=profile.path,
        dataset_fingerprint=fingerprint,
        steps=tuple(steps),
        issues=tuple(issues),
        gpu_hours=gpu_hours,
        final_state=state,
    )
