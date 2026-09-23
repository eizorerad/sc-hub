"""Our numbers next to a paper's: `bench.compare(ours, paper, source="Table 2, scGPT row")`.

Only the paper's metrics are compared (a metric we did not measure says so). Each
row has the difference, the relative difference and a verdict against a relative
tolerance (10% unless given, per metric if a dict). The table is printed, so a
finding can cite this cell for its numbers, and saved as work/compare/<name>.csv
and .json. A verdict on the reproduction is the student's and the agent's call,
written as a note; this only lays the numbers side by side.
"""

from __future__ import annotations

import csv
import math
import re
from typing import Any, Mapping

from .fsio import write_json_atomic

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
COLUMNS = ("metric", "paper", "ours", "diff", "relative", "tolerance", "verdict")


class CompareError(ValueError):
    pass


def _number(label: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CompareError(f"{label} is not a number: {value!r}") from exc
    if not math.isfinite(number):
        raise CompareError(f"{label} is not a finite number: {value!r}")
    return number


def _row(metric: str, paper: float, ours: float | None, tolerance: float) -> dict[str, Any]:
    if ours is None:
        return {"metric": metric, "paper": paper, "ours": None, "diff": None, "relative": None,
                "tolerance": tolerance, "verdict": "not measured"}
    diff = ours - paper
    relative = abs(diff) / abs(paper) if paper else None  # no relative difference from a zero
    within = relative <= tolerance + 1e-12 if relative is not None else diff == 0
    verdict = f"within {tolerance:.0%}" if within else "differs"
    return {"metric": metric, "paper": paper, "ours": ours, "diff": diff, "relative": relative,
            "tolerance": tolerance, "verdict": verdict}


def compare(ours: Mapping[str, Any], paper: Mapping[str, Any], source: str,
            tolerance: float | Mapping[str, float] = 0.10, name: str = "comparison") -> list[dict[str, Any]]:
    from .kernel_api import work_dir

    if not source.strip():
        raise CompareError("say where the paper's numbers come from (source='Table 2, row ...')")
    if not NAME.fullmatch(name):
        raise CompareError(f"name {name!r}: letters, digits, dot, dash and underscore only")
    default = tolerance if isinstance(tolerance, (int, float)) else 0.10
    per_metric = tolerance if isinstance(tolerance, Mapping) else {}
    rows = []
    for metric, value in paper.items():
        mine = ours.get(metric)
        rows.append(_row(str(metric), _number(f"paper[{metric!r}]", value),
                         None if mine is None else _number(f"ours[{metric!r}]", mine),
                         _number(f"tolerance[{metric!r}]", per_metric.get(metric, default))))
    folder = work_dir() / "compare"
    folder.mkdir(parents=True, exist_ok=True)
    write_json_atomic(folder / f"{name}.json", {"source": source, "rows": rows})
    with (folder / f"{name}.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(_table(rows, source))
    return rows


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{value:.4g}"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _table(rows: list[dict[str, Any]], source: str) -> str:
    lines = [f"paper: {source}", "| metric | paper | ours | diff | relative | verdict |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['metric']} | {_fmt(r['paper'])} | {_fmt(r['ours'])} | {_fmt(r['diff'])} | "
              f"{_percent(r['relative'])} | {r['verdict']} |" for r in rows]
    return "\n".join(lines)
