from __future__ import annotations

import hashlib
import os
from pathlib import Path

from schub.bench.filesnap import diff, scan


def test_diff_reports_created_modified_deleted_with_hashes(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "keep.txt").write_text("same")
    (tmp_path / "work" / "change.txt").write_text("old")
    (tmp_path / "work" / "gone.txt").write_text("bye")
    before = scan(tmp_path, max_files=100)
    (tmp_path / "work" / "change.txt").write_text("new content")
    os.utime(tmp_path / "work" / "change.txt", ns=(1, 1))
    (tmp_path / "work" / "gone.txt").unlink()
    (tmp_path / "work" / "table.csv").write_text("a,b\n1,2\n")
    changes = {c.path: c for c in diff(before, scan(tmp_path, max_files=100), tmp_path, hash_max_bytes=1 << 20)}
    assert set(changes) == {"work/change.txt", "work/gone.txt", "work/table.csv"}
    assert changes["work/table.csv"].change == "created"
    assert changes["work/table.csv"].sha256 == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert changes["work/change.txt"].change == "modified"
    assert changes["work/gone.txt"].change == "deleted"


def test_skips_journal_and_symlinked_folders(tmp_path: Path) -> None:
    project, elsewhere = tmp_path / "p", tmp_path / "huge"
    (project / "journal" / "cells").mkdir(parents=True)
    (project / "journal" / "cells" / "c0001.json").write_text("{}")
    elsewhere.mkdir()
    (elsewhere / "big.bin").write_text("x")
    (project / "data").symlink_to(elsewhere)
    (project / "notes.md").write_text("hi")
    assert set(scan(project, max_files=100).files) == {"notes.md"}


def test_big_files_are_not_hashed(tmp_path: Path) -> None:
    before = scan(tmp_path, max_files=10)
    (tmp_path / "big.h5ad").write_bytes(b"x" * 2048)
    change = diff(before, scan(tmp_path, max_files=10), tmp_path, hash_max_bytes=1024)[0]
    assert change.size == 2048 and change.sha256 is None


def test_truncated_scan_never_invents_deletions(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(str(i))
    full = scan(tmp_path, max_files=100)
    partial = scan(tmp_path, max_files=2)
    assert partial.truncated
    assert all(c.change != "deleted" for c in diff(full, partial, tmp_path, hash_max_bytes=0))
