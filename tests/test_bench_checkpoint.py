from __future__ import annotations

from pathlib import Path

import pytest

from schub.bench.checkpoint import CheckpointError, CheckpointStore
from schub.bench.models import WaitingJob


def test_default_is_active(tmp_path: Path) -> None:
    assert CheckpointStore(tmp_path).read().disposition == "active"


def test_waiting_needs_jobs_and_a_next_action(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path, now=lambda: "2026-09-24T10:00:00.000+00:00")
    with pytest.raises(CheckpointError, match="jobs"):
        store.write("waiting", next_action="read the QC table")
    with pytest.raises(CheckpointError, match="next action"):
        store.write("waiting", waiting_jobs=[WaitingJob(job_id="812")])
    saved = store.write("waiting", next_action="read the QC table", waiting_jobs=[WaitingJob(job_id="812")])
    assert store.read() == saved and saved.updated.startswith("2026-09-24")
    assert store.write("complete").disposition == "complete"
    with pytest.raises(CheckpointError):
        store.write("sleeping", next_action="x")


def test_unreadable_checkpoint_is_reported_not_fatal(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text('{"disposition": "on fire"}')
    assert "unreadable" in store.read().reason


def test_handoff_is_short(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    store.write_handoff("# Where we are\n- QC done on the twin\n")
    assert store.read_handoff().startswith("# Where we are")
    with pytest.raises(CheckpointError, match="120 lines"):
        store.write_handoff("\n".join(f"line {i}" for i in range(121)))
    with pytest.raises(CheckpointError):
        store.write_handoff("   ")
