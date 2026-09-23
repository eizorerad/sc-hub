"""Checks on plain files and tables."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..models import CheckResult
from . import CheckDef, failed, passed

PathOf = Callable[[str], Path]
MAX_ROWS_SCANNED = 5_000_000


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileParams(_Params):
    path: str
    min_bytes: int = Field(1, ge=0)


class ColumnsParams(_Params):
    path: str
    columns: list[str] = Field(min_length=1)
    min_rows: int = Field(1, ge=0)


class FiniteParams(_Params):
    path: str
    column: str


def _dialect(path: Path) -> str:
    return "\t" if path.suffix in (".tsv", ".tab") else ","


def check_file(path_of: PathOf, p: FileParams) -> CheckResult:
    path = path_of(p.path)
    size = path.stat().st_size
    if not path.is_file() or size < p.min_bytes:
        return failed("file", f"{p.path} has {size} bytes; expected at least {p.min_bytes}", size=size)
    return passed("file", f"{p.path}: {size} bytes", size=size)


def check_columns(path_of: PathOf, p: ColumnsParams) -> CheckResult:
    path = path_of(p.path)
    with path.open(newline="") as handle:
        reader = csv.reader(handle, delimiter=_dialect(path))
        header = next(reader, [])
        rows = sum(1 for _ in zip(range(MAX_ROWS_SCANNED), reader))
    missing = [c for c in p.columns if c not in header]
    if missing:
        return failed("table_columns", f"{p.path} lacks columns {missing}; it has {header[:30]}", rows=rows)
    if rows < p.min_rows:
        return failed("table_columns", f"{p.path} has {rows} rows; expected at least {p.min_rows}", rows=rows)
    return passed("table_columns", f"{p.path}: {rows} rows with {', '.join(p.columns)}", rows=rows)


def check_finite(path_of: PathOf, p: FiniteParams) -> CheckResult:
    path = path_of(p.path)
    bad = total = 0
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter=_dialect(path))
        if p.column not in (reader.fieldnames or []):
            return failed("finite_values", f"{p.path} has no column {p.column!r}")
        for row in zip(range(MAX_ROWS_SCANNED), reader):
            total += 1
            try:
                value = float(row[1][p.column])
            except (TypeError, ValueError):
                bad += 1
                continue
            bad += 0 if math.isfinite(value) else 1
    if total == 0 or bad:
        return failed("finite_values", f"{p.column}: {bad} of {total} values are NaN, infinite or not numbers",
                      bad=bad, total=total)
    return passed("finite_values", f"{p.column}: all {total} values finite", total=total)


CHECKS = (
    CheckDef("file", "the file exists and is not empty (min_bytes)", FileParams, check_file),
    CheckDef("table_columns", "a CSV/TSV has these columns and at least min_rows rows", ColumnsParams, check_columns),
    CheckDef("finite_values", "a CSV column has only finite numbers (e.g. a training loss)", FiniteParams, check_finite),
)
