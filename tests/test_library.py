from __future__ import annotations

import os
from dataclasses import replace

import pytest

from schub.datasets import dataset_fingerprint, list_datasets, write_catalog_entry
from schub.fetch import FetchError, fetch, missing
from schub.library import celltypist_dirs, find_asset, find_dataset, library_mode

from .conftest import library_datasets, make_adata


def put(write_h5ad, base, name, **meta):
    directory = base / name
    write_catalog_entry(directory, {"title": name, **meta})
    return write_h5ad(make_adata(), directory=directory)


@pytest.fixture
def locked(settings):
    """The shared library exists but this user cannot read it."""
    os.chmod(settings.library, 0)
    yield settings
    os.chmod(settings.library, 0o755)


def test_shared_library_wins_over_local_and_private(settings, write_h5ad):
    put(write_h5ad, library_datasets(settings), "kang2018")
    put(write_h5ad, settings.local_library / "datasets", "kang2018")
    put(write_h5ad, settings.data_dir, "mine")
    assert find_dataset(settings, "kang2018").source == "library"
    assert find_dataset(settings, "mine").source == "private"
    assert find_dataset(settings, "absent") is None
    names = {(d.name, d.source) for d in list_datasets(settings)}
    assert names == {("kang2018", "library"), ("mine", "private")}
    assert "shared library at" in library_mode(settings)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_library_falls_back_to_local(locked, write_h5ad):
    settings = locked
    put(write_h5ad, settings.local_library / "datasets", "kang2018")
    assert settings.shared_library is None
    assert settings.library_roots == (settings.local_library,)
    assert find_dataset(settings, "kang2018").source == "local"
    assert celltypist_dirs(settings) == (settings.local_library / "models" / "celltypist" / "data" / "models",)
    assert "fallback" in library_mode(settings)


def test_no_library_configured(settings):
    alone = replace(settings, library=None)
    assert alone.library_roots == (alone.local_library,)
    assert "local only" in library_mode(alone)


def test_content_fingerprint_matches_across_copies(settings, write_h5ad):
    shared = put(write_h5ad, library_datasets(settings), "kang2018")
    local = settings.local_library / "datasets" / "kang2018" / "data.h5ad"
    local.parent.mkdir(parents=True)
    local.write_bytes(shared.read_bytes())
    for copy in (shared, local):
        write_catalog_entry(copy.parent, {"file_sha256": "abc", "file_size": copy.stat().st_size})
    roots = settings.library_roots
    assert dataset_fingerprint(shared, roots) == dataset_fingerprint(local, roots)
    assert dataset_fingerprint(shared) != dataset_fingerprint(local)  # untrusted: path-based
    os.utime(local, (local.stat().st_atime, local.stat().st_mtime + 100))  # edited after the catalog
    assert dataset_fingerprint(shared, roots) != dataset_fingerprint(local, roots)
    write_catalog_entry(local.parent, {"file_sha256": "abc", "file_size": 1})  # stale size
    assert dataset_fingerprint(shared, roots) != dataset_fingerprint(local, roots)


def test_fetch_downloads_only_missing_assets_into_local_library(settings, write_h5ad, monkeypatch):
    put(write_h5ad, library_datasets(settings), "kang2018")
    calls = []

    def fake(into, scratch):
        calls.append(into)
        target = into / "datasets" / "pbmc3k" / "data.h5ad"
        target.parent.mkdir(parents=True)
        target.write_text("x")
        return target

    monkeypatch.setitem(__import__("schub.fetch", fromlist=["FETCHERS"]).FETCHERS, "pbmc3k", fake)
    assert missing(settings, ["kang2018", "pbmc3k"]) == ["pbmc3k"]
    report = fetch(settings, ["kang2018", "pbmc3k"])
    assert report["kang2018"].startswith("present in library")
    assert report["pbmc3k"].startswith("downloaded")
    assert calls == [settings.local_library]
    assert find_asset(settings, "pbmc3k").source == "local"
    with pytest.raises(ValueError, match="unknown assets"):
        fetch(settings, ["nope"])


def test_download_verifies_checksum(tmp_path, monkeypatch):
    import io

    from schub import fetch as fetch_module

    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"payload"))
    with pytest.raises(FetchError, match="checksum mismatch"):
        fetch_module._download("https://example.org/x", tmp_path / "x", "0" * 64)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_locked_parent_directory_does_not_crash(settings, tmp_path, write_h5ad):
    parent = tmp_path / "owner-home"
    library = parent / "sc-hub-library"
    (library / "datasets").mkdir(parents=True)
    settings = replace(settings, library=library)
    put(write_h5ad, settings.local_library / "datasets", "kang2018")
    os.chmod(parent, 0)
    try:
        assert settings.shared_library is None
        assert find_dataset(settings, "kang2018").source == "local"
        assert missing(settings, ["kang2018"]) == []
    finally:
        os.chmod(parent, 0o755)


def test_celltypist_needs_every_model_file(settings):
    folder = celltypist_dirs(settings)[0]
    folder.mkdir(parents=True)
    (folder / "Immune_All_Low.pkl").write_bytes(b"m")
    assert find_asset(settings, "celltypist") is None
    (folder / "Immune_All_High.pkl").write_bytes(b"m")
    assert find_asset(settings, "celltypist").source == "library"
