"""Checks a cell asks for (`run(..., checks=[...])`) and the bench runs on its outputs.

A check earns its place only if it can verify what it claims: each one here has a
known-bad example in the tests that it must fail on. A failed check does not undo
the cell; it marks the result so nobody builds on it unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import BaseModel, ValidationError

from ...config import Settings
from ..files import FilesError, resolve
from ..models import CheckResult, CheckSpec


@dataclass(frozen=True)
class CheckDef:
    name: str
    summary: str
    params: type[BaseModel]
    run: Callable[[Callable[[str], Path], Any], CheckResult]
    where: str = "anywhere"  # "job": only meaningful inside a Slurm job (e.g. GPU visible)


def _registry() -> dict[str, CheckDef]:
    from . import h5ad, perturb, tables, training

    found: dict[str, CheckDef] = {}
    for module in (tables, h5ad, perturb, training):
        for check in module.CHECKS:
            found[check.name] = check
    return found


def registry() -> dict[str, CheckDef]:
    return _registry()


ALIASES = {"file_exists": "file", "exists": "file"}  # names agents reach for first (seen in the evaluation)


def canonical(specs: Sequence[CheckSpec]) -> tuple[CheckSpec, ...]:
    """The same checks under their registered names."""
    return tuple(s.model_copy(update={"name": ALIASES[s.name]}) if s.name in ALIASES else s for s in specs)


def run_checks(settings: Settings, project: str, specs: Sequence[CheckSpec]) -> tuple[CheckResult, ...]:
    checks = registry()
    return tuple(_run_one(settings, project, checks, spec) for spec in specs)


def _run_one(settings: Settings, project: str, checks: dict[str, CheckDef], spec: CheckSpec) -> CheckResult:
    check = checks.get(spec.name)
    if check is None:
        return CheckResult(name=spec.name, status="error", message=f"no such check; available: {', '.join(checks)}")
    try:
        params = check.params.model_validate(spec.params)
    except ValidationError as exc:
        return CheckResult(name=spec.name, status="error", message=f"bad params: {exc.errors()[0]['msg']}")

    def path_of(raw: str) -> Path:
        return resolve(settings, project, raw)

    try:
        return check.run(path_of, params)
    except FilesError as exc:
        return CheckResult(name=spec.name, status="fail", message=str(exc))
    except Exception as exc:  # noqa: BLE001 - a broken check is reported, never fatal
        return CheckResult(name=spec.name, status="error", message=f"{type(exc).__name__}: {exc}")


def describe() -> str:
    """Markdown list of the checks with their parameters (the `checks` skill)."""
    lines = ["# Checks", "", "Pass them to run(..., checks=[{\"name\": ..., \"params\": {...}}]). Paths are relative "
             "to the project folder ('work/table.csv'), while a cell starts in work/ (it writes 'table.csv'). "
             "A failed check marks the cell; do not build on it until it passes.", ""]
    for check in registry().values():
        fields = ", ".join(
            f"{name}{'' if field.is_required() else '=' + repr(field.default)}"
            for name, field in check.params.model_fields.items()
        )
        where = " (inside a Slurm job)" if check.where == "job" else ""
        lines.append(f"- `{check.name}({fields})`{where}: {check.summary}")
    return "\n".join(lines) + "\n"


def passed(name: str, message: str, **details: Any) -> CheckResult:
    return CheckResult(name=name, status="pass", message=message, details=details)


def failed(name: str, message: str, **details: Any) -> CheckResult:
    return CheckResult(name=name, status="fail", message=message, details=details)
