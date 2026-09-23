"""What a run already computed, as notebook outputs.

A downloaded notebook then reads like one that was run: each step's numbers and
figures, the top of its DE table and what the final AnnData holds, taken from the
pipeline's saved results (nothing is recomputed). Running a cell replaces its
outputs with what it computes there.

Readers never raise: a missing file just means no output. A file that exists but
cannot be read (Lustre sometimes fails a read) is noted in `problems`, so the
dashboard renders that notebook again on its next build instead of keeping a gap.
"""

from __future__ import annotations

import base64
import csv
import json
from html import escape
from pathlib import Path
from typing import Any

MAX_FIGURE_BYTES = 2_000_000  # a larger figure is left out, with a note
FIGURE_BUDGET_BYTES = 8_000_000  # per notebook: the laptop mirrors a dozen of them
TABLE_ROWS = 30
SCALARS = (str, int, float, bool)
HIDDEN = {"warnings", "figure"}  # shown as themselves (warning lines, the figure)

Problems = list[str]


class Budget:
    """Bytes of figures one notebook may still embed."""

    def __init__(self, total: int = FIGURE_BUDGET_BYTES) -> None:
        self.left = total


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def stream(text: str, name: str = "stdout") -> dict[str, Any]:
    return {"output_type": "stream", "name": name, "text": _lines(text if text.endswith("\n") else text + "\n")}


def display(data: dict[str, Any]) -> dict[str, Any]:
    return {"output_type": "display_data", "data": data, "metadata": {}}


def _cell_text(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return "" if value is None else str(value)


def table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """An HTML table (escaped) with a plain-text twin, like a DataFrame's output."""
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(_cell_text(v))}</td>" for v in row) + "</tr>" for row in rows)
    text = "\n".join("  ".join(_cell_text(v) for v in row) for row in [headers, *rows])
    return display({"text/html": _lines(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"),
                    "text/plain": _lines(text)})


def warning_lines(summary: dict[str, Any] | None) -> list[str]:
    """A summary's warnings, whatever shape an older brick wrote them in."""
    raw = (summary or {}).get("warnings")
    if isinstance(raw, str):
        return [raw]
    return [w for w in raw if isinstance(w, str)] if isinstance(raw, list) else []


def summary_rows(summary: dict[str, Any] | None) -> list[tuple[str, Any]]:
    """The numbers of a step's summary worth a table row."""
    return [(k, v) for k, v in (summary or {}).items() if k not in HIDDEN and isinstance(v, SCALARS)]


def _read_summary(path: Path, problems: Problems) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        problems.append(f"{path}: {exc}")
        return None
    return loaded if isinstance(loaded, dict) else None


def _figures(results: Path, budget: Budget, problems: Problems) -> list[dict[str, Any]]:
    found = sorted(p for p in results.glob("*.png") if not p.stem.endswith("_thumb")) if results.is_dir() else []
    outputs = []
    for path in found:
        try:
            size = path.stat().st_size
            if size > min(MAX_FIGURE_BYTES, budget.left):
                why = "too large" if size > MAX_FIGURE_BYTES else "this notebook already holds enough figures"
                outputs.append(stream(f"({path.name}, {size / 1e6:.1f} MB, left out: {why}; it is in {results})"))
                continue
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            continue
        budget.left -= size
        outputs.append(display({"image/png": encoded, "text/plain": [f"<{path.name}>"]}))
    return outputs


def step_outputs(step_dir: str, budget: Budget, problems: Problems) -> list[dict[str, Any]]:
    """The saved result of one step: its numbers, warnings and figures."""
    results = Path(step_dir) / "results"
    summary = _read_summary(results / "summary.json", problems)
    if summary is None:
        return []
    outputs = [stream("Saved result of this step in the pipeline. Run this cell to compute it here again.")]
    rows = summary_rows(summary)
    if rows:
        outputs.append(table(("result", "pipeline"), rows))
    warnings = warning_lines(summary)
    if warnings:
        outputs.append(stream("".join(f"warning: {w}\n" for w in warnings), name="stderr"))
    return outputs + _figures(results, budget, problems)


def csv_head(path: Path, problems: Problems, rows: int = TABLE_ROWS) -> list[dict[str, Any]]:
    """The first rows of a result table, as `pd.read_csv(...).head()` shows them."""
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.reader(handle)
            headers = next(reader, None)
            body = [tuple(row) for _, row in zip(range(rows), reader)]
    except FileNotFoundError:
        return []
    except (OSError, csv.Error) as exc:
        problems.append(f"{path}: {exc}")
        return []
    return [table(tuple(headers), body)] if headers and body else []


def _length(node: Any) -> int:
    """Rows of an index: a plain dataset, or (newer anndata) a string/categorical array group."""
    if hasattr(node, "shape"):
        return int(node.shape[0])
    for part in ("values", "codes"):
        if part in node:
            return int(node[part].shape[0])
    raise ValueError("unknown index encoding")


def anndata_summary(path: Path, problems: Problems) -> list[dict[str, Any]]:
    """What `adata` prints (shape and the names of its parts), read from the file's
    metadata only: the matrix and layers are never loaded."""
    if not path.is_file():
        return []
    try:
        import h5py

        from .h5ad_profile import UnsupportedFile, reject_external_storage

        with h5py.File(path, "r") as handle:
            try:
                reject_external_storage(handle)
            except UnsupportedFile:
                return []  # links out of the file are not followed, as everywhere else
            parts: dict[str, list[str]] = {}
            sizes = []
            for group in ("obs", "var"):
                node = handle[group]
                index = str(node.attrs.get("_index", "_index"))
                order = node.attrs.get("column-order", [k for k in node.keys() if k not in (index, "__categories")])
                parts[group] = [str(c) for c in order]
                sizes.append(_length(node[index]))
            for group in ("uns", "obsm", "varm", "layers", "obsp", "varp"):
                parts[group] = sorted(handle[group].keys()) if group in handle else []
    except Exception as exc:  # noqa: BLE001 - an unreadable file only means no summary (retried later)
        problems.append(f"{path}: {exc}")
        return []
    n_obs, n_vars = sizes
    lines = [f"AnnData object with n_obs × n_vars = {n_obs} × {n_vars}"]
    lines += [f"    {group}: {', '.join(repr(n) for n in names)}" for group, names in parts.items() if names]
    return [display({"text/plain": _lines("\n".join(lines))})]
