from __future__ import annotations

import json
import os
from pathlib import Path

from schub.bench import fsio


def test_atomic_write_replaces_whole_file(tmp_path: Path) -> None:
    target = tmp_path / "a" / "entry.json"
    fsio.write_json_atomic(target, {"n": 1})
    fsio.write_json_atomic(target, {"n": 2})
    assert json.loads(target.read_text()) == {"n": 2}
    assert [p.name for p in target.parent.iterdir()] == ["entry.json"]  # no temp files left


def test_exclusive_create_refuses_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "note.json"
    assert fsio.create_json_exclusive(target, {"first": True}) is True
    assert fsio.create_json_exclusive(target, {"first": False}) is False
    assert json.loads(target.read_text()) == {"first": True}
    assert [p.name for p in tmp_path.iterdir()] == ["note.json"]


def test_exclusive_create_is_complete_or_absent(tmp_path: Path, monkeypatch) -> None:
    """A reader never sees a half-written exclusive file: it appears by link()."""
    target = tmp_path / "x.json"

    def broken_link(src: str, dst: str) -> None:
        raise OSError(1, "not supported")

    monkeypatch.setattr(os, "link", broken_link)
    assert fsio.create_json_exclusive(target, {"ok": 1}) is True  # falls back to O_EXCL
    assert json.loads(target.read_text()) == {"ok": 1}
    assert fsio.create_json_exclusive(target, {"ok": 2}) is False


def test_read_json_tolerates_missing_and_garbage(tmp_path: Path) -> None:
    assert fsio.read_json(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert fsio.read_json(tmp_path / "bad.json") is None
    (tmp_path / "list.json").write_text("[1, 2]")
    assert fsio.read_json(tmp_path / "list.json") is None  # records are objects
