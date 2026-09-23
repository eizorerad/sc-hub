"""Which earlier steps a result actually depends on.

A step key chains every step before it, so a branch that changes scVI's latent
size re-runs its pseudobulk DE. But DE reads only raw counts and a few obs
columns: when none of the steps since the counts last changed wrote those
columns, the DE result cannot differ between such variants. Students then
compare branches that are identical by construction (Kang 2018: n_latent 10 vs
30 gave byte-identical DE tables). The planner says so instead of letting the
graph suggest otherwise.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from .bricks import BrickSpec
from .bricks.base import ColumnsFn
from .state import Issue, warning


class Step(Protocol):
    """What this check needs of a planned step."""

    index: int
    brick: str
    params: dict[str, Any]


def _columns(fn: ColumnsFn | None, params: dict[str, Any], spec: BrickSpec) -> tuple[str, ...]:
    """A spec's column callback, called with the typed params (plans store them as JSON)."""
    if fn is None:
        return ()
    return tuple(c for c in fn(spec.params_model.model_validate(params)) if c)


def _steps_text(indexes: Sequence[int], names: Sequence[str]) -> str:
    where = f"step {indexes[0]}" if len(indexes) == 1 else f"steps {indexes[0]}-{indexes[-1]}"
    return f"{where} ({', '.join(names)})"


def _unused_before(position: int, steps: Sequence[Step], registry: Mapping[str, BrickSpec]) -> list[Step] | None:
    """Steps between the last change of the counts and `position`, if none wrote what it reads."""
    step = steps[position]
    spec = registry[step.brick]
    reads = set(_columns(spec.reads_obs, step.params, spec))
    between: list[Step] = []
    for expected, earlier in enumerate(reversed(steps[:position]), start=1):
        if earlier.index != step.index - expected:
            return None  # a step in between was not planned (bad params): unknown effect
        earlier_spec = registry.get(earlier.brick)
        if earlier_spec is None or not earlier_spec.keeps_counts:
            break
        if reads & set(_columns(earlier_spec.writes_obs, earlier.params, earlier_spec)):
            return None  # it reads something computed here, so everything before may matter
        between.insert(0, earlier)
    return between


def upstream_issues(steps: Sequence[Step], registry: Mapping[str, BrickSpec]) -> list[Issue]:
    """One warning per step whose result cannot depend on the analysis steps before it.

    Routine preparation alone (normalize, then DE: the course recipe) is not worth a warning.
    """
    issues: list[Issue] = []
    for position, step in enumerate(steps):
        spec = registry.get(step.brick)
        if spec is None or spec.reads_obs is None:
            continue
        unused = _unused_before(position, steps, registry)
        if not unused or all(registry[s.brick].prepares for s in unused):
            continue
        reads = ", ".join(_columns(spec.reads_obs, step.params, spec))
        labels = [c for s in unused for c in _columns(registry[s.brick].label_columns, s.params, registry[s.brick])]
        hint = f" To test per the labels this branch computes, group by {' or '.join(labels)}." if labels else ""
        issues.append(warning(
            "upstream_unused",
            f"{step.brick} reads only raw counts and the columns {reads}; "
            f"{_steps_text([s.index for s in unused], [s.brick for s in unused])} change none of them, "
            f"so variants of those steps give the same result.{hint}",
            step.index,
        ))
    return issues
