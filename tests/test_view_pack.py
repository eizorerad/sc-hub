"""The Windows mirror's view of the dashboard: a checksum of the heavy files and a tar stream, page last."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from schub.dashboard.pack import heavy_sum, pack


def view(tmp_path: Path) -> Path:
    root = tmp_path / "view"
    for relative, text in (("index.html", "<html>"), ("versions.json", "{}"), ("jproj/a.js", "x"),
                           ("jfig/a/c0001.png", "png"), ("jnb/a.js", "nb"), ("img/r1.png", "img"),
                           (".schub-view", ""), ("jproj/b.js.tmp", "half")):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def members(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return tar.getnames()


def test_light_is_the_page_and_its_scripts_full_is_everything_and_the_page_comes_last(tmp_path: Path) -> None:
    root = view(tmp_path)
    light, full = io.BytesIO(), io.BytesIO()
    pack(root, "light", light)
    pack(root, "full", full)
    assert members(light.getvalue()) == ["jproj/a.js", "versions.json", "index.html"]
    names = members(full.getvalue())
    assert names[-1] == "index.html" and {"jfig/a/c0001.png", "jnb/a.js", "img/r1.png"} <= set(names)
    assert ".schub-view" not in names and "jproj/b.js.tmp" not in names  # the marker and half-written files stay


def test_the_sum_follows_the_heavy_files_only(tmp_path: Path) -> None:
    root = view(tmp_path)
    before = heavy_sum(root)
    (root / "index.html").write_text("<html>new")  # the page changes every minute: no full download for it
    (root / "jproj" / "a.js").write_text("y")
    assert heavy_sum(root) == before
    figure = root / "jfig" / "a" / "c0001.png"
    figure.write_text("a new figure")
    os.utime(figure, ns=(1, 2))
    assert heavy_sum(root) != before


def test_only_light_or_full(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pack(view(tmp_path), "../etc", io.BytesIO())
