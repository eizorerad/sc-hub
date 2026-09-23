"""What a generated notebook imports: the pipeline's steps as they were recorded,
so any cell can re-run one step with the brick's own code.

A step reads the result of the step before it: this notebook's own result when
the student re-ran that step here, otherwise the pipeline's saved result. So a
notebook can start at any step, and heavy steps (FASTQ counting, scVI) need not
run again. Everything the notebook writes goes to its `work` folder, never into
the pipeline's cache.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bricks.base import StepIO
from .notebook_results import summary_rows, warning_lines
from .state import DatasetState
from .stepfile import STEP_FILE, SUCCESS, SUMMARY_FILE



class NotebookError(RuntimeError):
    """Something the student can fix in the notebook (e.g. run an earlier step first)."""


@dataclass(frozen=True)
class RecordedStep:
    index: int
    brick: str
    step_dir: Path

    @property
    def saved_output(self) -> Path:
        return self.step_dir / "output.h5ad"

    @property
    def completed(self) -> bool:
        return (self.step_dir / SUCCESS).exists()


class Pipeline:
    def __init__(self, steps: list[tuple[int, str, str]], work: str | Path) -> None:
        self.steps = {index: RecordedStep(index, brick, Path(folder)) for index, brick, folder in steps}
        # Absolute: tools a brick starts in another folder (Cell Ranger) and a later %cd see the same place.
        self.work = Path(work).resolve()

    # ---- where things are -------------------------------------------------------

    def _step(self, index: int) -> RecordedStep:
        if index not in self.steps:
            raise NotebookError(f"this pipeline has no step {index} (steps: {sorted(self.steps)})")
        return self.steps[index]

    def _record(self, index: int) -> dict[str, Any]:
        path = self._step(index).step_dir / STEP_FILE
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise NotebookError(f"step {index}: its record {path} cannot be read ({exc})") from exc

    def folder(self, index: int) -> Path:
        """This notebook's folder for step `index`, laid out like the pipeline's step folder."""
        step = self._step(index)
        return self.work / f"{index}_{step.brick}"

    def mine(self, index: int) -> Path:
        """Where this notebook writes step `index`'s output."""
        return self.folder(index) / "output.h5ad"

    def results_dir(self, index: int) -> Path:
        return self.folder(index) / "results"

    def saved_results(self, index: int) -> Path:
        """The pipeline's own results folder for step `index` (tables, figures, models)."""
        return self._step(index).step_dir / "results"

    def file(self, index: int, name: str) -> Path:
        """A result of step `index` (a table, a model folder): yours if you re-ran it, else the pipeline's."""
        mine = self.results_dir(index) / name
        return mine if mine.exists() else self.saved_results(index) / name

    def _upstream(self, index: int, source: Path) -> RecordedStep | None:
        """The earlier step whose output this step reads (paths compared resolved: symlinked roots)."""
        wanted = source.resolve()
        earlier = [s for s in self.steps.values() if s.index < index and s.saved_output.resolve() == wanted]
        return max(earlier, key=lambda s: s.index) if earlier else None

    def _input(self, index: int, recorded: Path) -> tuple[Path, str]:
        upstream = self._upstream(index, recorded)
        if upstream is None:
            return recorded, f"the dataset {recorded}"
        if self.mine(upstream.index).is_file():
            return self.mine(upstream.index), f"your result of step {upstream.index} ({upstream.brick}) in this notebook"
        if recorded.is_file():
            return recorded, f"the pipeline's saved result of step {upstream.index} ({upstream.brick})"
        raise NotebookError(
            f"step {index} reads the result of step {upstream.index} ({upstream.brick}), which is not there: "
            f"run step {upstream.index} above first."
        )

    # ---- what the cells call ------------------------------------------------------

    def io(self, index: int) -> StepIO:
        """Inputs and outputs for re-running step `index` here."""
        record = self._record(index)
        source, described = self._input(index, Path(record["input"]))
        results = self.results_dir(index)
        results.mkdir(parents=True, exist_ok=True)
        print(f"Step {index} reads {described}.")
        return StepIO(
            input=source,
            output=None if record.get("output") is None else self.mine(index),
            results_dir=results,
            state_in=DatasetState.model_validate(record["state_in"]),
            context=dict(record.get("context", {})),
        )

    def load(self, index: int) -> Any:
        """The AnnData after step `index`: yours if you re-ran it here, else the pipeline's."""
        import anndata as ad

        _inline_plots()
        path = self.mine(index) if self.mine(index).is_file() else self._step(index).saved_output
        if not path.is_file():
            raise NotebookError(f"step {index} has no output .h5ad (it writes tables only, or has not run yet)")
        return ad.read_h5ad(path)

    def show(self, index: int, summary: dict[str, Any] | None = None) -> None:
        """This step's numbers next to the pipeline's, and its figures."""
        _inline_plots()
        pipeline = _read_json(self.saved_results(index) / SUMMARY_FILE)
        rows = [(key, _scalar(value), _scalar(pipeline.get(key)) if pipeline else "") for key, value in summary_rows(summary)]
        _display_table(rows)
        for warning in warning_lines(summary):
            print(f"warning: {warning}", file=sys.stderr)
        for figure in sorted(self.results_dir(index).glob("*.png")):
            if not figure.stem.endswith("_thumb"):
                _display_image(figure)

    def overview(self) -> None:
        """Every step: did the pipeline finish it, and have you re-run it here."""
        rows = [
            (f"{s.index}. {s.brick}", "saved" if s.completed else "not in the cache",
             "yes" if self.mine(s.index).is_file() or any(self.results_dir(s.index).glob("*")) else "")
            for s in sorted(self.steps.values(), key=lambda s: s.index)
        ]
        _display_table(rows, ("step", "pipeline result", "re-run here"))

    def reset(self, index: int | None = None) -> None:
        """Forget what this notebook computed (one step or all), so steps read the pipeline's results again."""
        indices = [index] if index is not None else list(self.steps)
        for i in indices:
            shutil.rmtree(self.folder(i), ignore_errors=True)


def _inline_plots() -> None:
    """Bricks switch matplotlib to Agg (they run in jobs); give the notebook inline plots back."""
    try:
        from IPython import get_ipython
    except ImportError:
        return
    shell = get_ipython()
    if shell is not None and getattr(shell, "kernel", None) is not None:
        shell.run_line_magic("matplotlib", "inline")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _scalar(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return "" if value is None else str(value)


def _display_table(rows: list[tuple[str, ...]], headers: tuple[str, ...] = ("result", "this notebook", "pipeline")) -> None:
    try:
        import pandas as pd
        from IPython.display import display
    except ImportError:
        for row in rows:
            print(" | ".join(row))
        return
    if rows:
        display(pd.DataFrame(rows, columns=list(headers)).set_index(headers[0]))


def _display_image(path: Path) -> None:
    try:
        from IPython.display import Image, display
    except ImportError:
        print(f"figure: {path}")
        return
    display(Image(filename=str(path)))
