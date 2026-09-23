"""The contract between the submitting side and the job: one JSON per step."""

from __future__ import annotations

from typing import Any

from .state import DatasetState, Frozen

STEP_FILE = "step.json"
SUCCESS = "_SUCCESS"
ERROR_FILE = "error.json"
JOB_ID_FILE = "job_id"
SUMMARY_FILE = "summary.json"


class StepFile(Frozen):
    brick: str
    version: str
    code_id: str = ""
    params: dict[str, Any]
    input: str
    output: str | None
    results_dir: str
    state_in: DatasetState
    context: dict[str, str]
