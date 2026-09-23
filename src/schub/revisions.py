"""Fixing a step versus trying an alternative from a step.

- revise: the branch stays the same branch and gets a new revision (r2, r3...);
  the old revision is kept with the reason for the change. Use it when a step
  was wrong ("the threshold at step 2 is too strict").
- fork: a new branch that copies the parent's steps up to step N and changes N
  (and optionally everything after it). The parent is untouched. Use it for
  "from here on, try it differently".

Steps are addressed 1-based, as the dashboard numbers them; a step reference is
"<project>/<branch>#<step>".
"""

from __future__ import annotations

import re
from typing import Any

from .bricks import REGISTRY
from .planner import StepRequest
from .projects import BranchSpec, ProjectError, ProjectStore
from .state import Frozen

STEP_REF = re.compile(r"^(?P<project>[a-z0-9][a-z0-9_/-]*)/(?P<branch>[a-z0-9][a-z0-9_-]*)#(?P<step>\d{1,2})$")


class Revision(Frozen):
    revision: int
    saved: str
    reason: str
    forked_from: str | None = None
    changes: tuple[str, ...] = ()


def parse_step_ref(ref: str) -> tuple[str, str, int]:
    match = STEP_REF.fullmatch(ref.strip())
    if match is None:
        raise ProjectError(f"step reference '{ref}' must look like <project>/<branch>#<step>, e.g. pbmc/main#4")
    return match["project"], match["branch"], int(match["step"])


def _param_changes(where: str, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    keys = sorted(set(old) | set(new))
    return [f"{where}: {k} {old.get(k, 'default')} → {new.get(k, 'default')}" for k in keys if old.get(k) != new.get(k)]


def _step_changes(old: tuple[StepRequest, ...], new: tuple[StepRequest, ...], label: str = "step") -> list[str]:
    changes = []
    for index in range(max(len(old), len(new))):
        a = old[index] if index < len(old) else None
        b = new[index] if index < len(new) else None
        where = f"{label} {index + 1}"
        if a is None and b is not None:
            changes.append(f"{where}: added {b.brick}")
        elif b is None and a is not None:
            changes.append(f"{where}: removed {a.brick}")
        elif a is not None and b is not None and a.brick != b.brick:
            changes.append(f"{where}: {a.brick} → {b.brick}")
        elif a is not None and b is not None:
            changes += _param_changes(f"{where} {b.brick}", a.params, b.params)
    return changes


def describe_changes(old: BranchSpec, new: BranchSpec) -> list[str]:
    """Human-readable differences between two revisions of a branch."""
    changes = []
    if old.dataset != new.dataset:
        changes.append(f"dataset: {old.dataset} → {new.dataset}")
    if old.from_branch != new.from_branch:
        changes.append(f"based on: {old.from_branch or 'own steps'} → {new.from_branch or 'own steps'}")
    changes += _step_changes(old.steps, new.steps)
    for key in sorted(set(old.overrides) | set(new.overrides)):
        where = f"step {key}" if key.isdigit() else key
        changes += _param_changes(f"override {where}", old.overrides.get(key, {}), new.overrides.get(key, {}))
    changes += _step_changes(old.append, new.append, label="appended step")
    return changes or ["no pipeline change (metadata only)"]


def history(store: ProjectStore, project: str, branch: str) -> list[Revision]:
    specs = store.revisions(project, branch)
    found = []
    for index, spec in enumerate(specs):
        changes = describe_changes(specs[index - 1], spec) if index else ["first revision"]
        found.append(Revision(revision=spec.revision, saved=spec.saved, reason=spec.reason,
                              forked_from=spec.forked_from, changes=tuple(changes)))
    return found


def _patched(params: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Merge a params patch; None resets a parameter to its default."""
    merged = {**params, **changes}
    return {k: v for k, v in merged.items() if v is not None}


def _defaults_for(brick: str, params: dict[str, Any]) -> dict[str, Any]:
    """A patch in which None means "this brick's default", written out explicitly (an override
    must win over a parent's or brick-wide value, which dropping the key would not do)."""
    from .bricks import REGISTRY

    fields = REGISTRY[brick].params_model.model_fields if brick in REGISTRY else {}
    patch = {}
    for key, value in params.items():
        if value is not None:
            patch[key] = value
            continue
        field = fields.get(key)
        if field is None or field.is_required():
            raise ProjectError(f"{brick}.{key} has no default to reset to")
        patch[key] = field.get_default(call_default_factory=True)
    return patch


def _check_step(step: int, count: int) -> None:
    if not 1 <= step <= count:
        raise ProjectError(f"step {step} does not exist; the branch has steps 1-{count}")


def _sweep_after(spec: BranchSpec, step: int, params: dict[str, Any], brick: str, brick_replaced: bool) -> dict[str, Any]:
    """A revision of a sweep's varied step keeps the sweep fields true: the new value (the
    default when it is reset with None), or no sweep at all once the brick is replaced."""
    if spec.sweep is None or step != spec.sweep_step:
        return {}
    if brick_replaced:
        return {"sweep": None, "sweep_step": None, "sweep_param": None, "sweep_value": None}
    if spec.sweep_param not in params:
        return {}
    value = params[spec.sweep_param]
    field = REGISTRY[brick].params_model.model_fields.get(spec.sweep_param or "") if brick in REGISTRY else None
    return {"sweep_value": field.default if value is None and field is not None else value}


def revised_spec(
    store: ProjectStore, project: str, branch: str, step: int,
    params: dict[str, Any] | None = None, brick: str | None = None,
) -> BranchSpec:
    """The branch with step `step` changed (params patched, or the brick replaced)."""
    spec = store.load_branch(project, branch)
    revised = _revised(store, project, branch, step, params, brick)  # checks that the step exists
    current = store.resolve(project, branch).steps[step - 1].brick
    replaced = bool(brick) and brick != current
    return revised.model_copy(update=_sweep_after(spec, step, params or {}, current, replaced))


def _revised(
    store: ProjectStore, project: str, branch: str, step: int,
    params: dict[str, Any] | None = None, brick: str | None = None,
) -> BranchSpec:
    spec = store.load_branch(project, branch)
    resolved = store.resolve(project, branch)
    _check_step(step, len(resolved.steps))
    current = resolved.steps[step - 1]
    params = params or {}
    if brick and brick != current.brick:
        # A different brick cannot be expressed as an override: write out the steps.
        steps = list(resolved.steps)
        steps[step - 1] = StepRequest(brick=brick, params=_patched({}, params))
        # Written out in full: the branch no longer follows its parent (from:).
        return BranchSpec(dataset=resolved.dataset, steps=tuple(steps), species=resolved.species,
                          gene_ids=resolved.gene_ids, idea=spec.idea, description=spec.description,
                          forked_from=spec.forked_from, sweep=spec.sweep, sweep_step=spec.sweep_step,
                          sweep_param=spec.sweep_param, sweep_value=spec.sweep_value)
    inherited = len(resolved.steps) - len(spec.append)
    if step <= inherited and not spec.from_branch and not spec.overrides:
        steps = list(spec.steps)
        steps[step - 1] = steps[step - 1].model_copy(update={"params": _patched(steps[step - 1].params, params)})
        return spec.model_copy(update={"steps": tuple(steps)})
    if step <= inherited:
        key = str(step)
        overrides = {**spec.overrides, key: {**spec.overrides.get(key, {}), **_defaults_for(current.brick, params)}}
        return spec.model_copy(update={"overrides": overrides})
    appended = list(spec.append)
    position = step - inherited - 1
    appended[position] = appended[position].model_copy(update={"params": _patched(appended[position].params, params)})
    return spec.model_copy(update={"append": tuple(appended)})


def forked_spec(
    store: ProjectStore, project: str, branch: str, step: int,
    params: dict[str, Any] | None = None, brick: str | None = None,
    then: tuple[StepRequest, ...] | None = None, reason: str = "",
) -> BranchSpec:
    """A new branch: the parent's steps before `step`, a changed step `step`, then either
    the parent's remaining steps or `then`."""
    parent = store.load_branch(project, branch)
    resolved = store.resolve(project, branch)
    _check_step(step, len(resolved.steps))
    current = resolved.steps[step - 1]
    changed = (
        StepRequest(brick=brick, params=_patched({}, params or {}))
        if brick and brick != current.brick
        else current.model_copy(update={"params": _patched(current.params, params or {})})
    )
    rest = then if then is not None else resolved.steps[step:]
    return BranchSpec(
        dataset=resolved.dataset,
        steps=(*resolved.steps[: step - 1], changed, *rest),
        species=resolved.species,
        gene_ids=resolved.gene_ids,
        idea=parent.idea,
        description=f"fork of {branch} at step {step}" + (f": {reason}" if reason else ""),
        forked_from=f"{branch}@r{parent.revision}#{step}",
        reason=reason,
    )
