from __future__ import annotations

from pathlib import Path

import pytest

from schub.bench.config import BenchConfig, load_bench_config
from schub.config import load_settings


def test_defaults_leave_room_for_one_batch_job() -> None:
    config = BenchConfig()
    assert config.cpus <= 16 and config.mem_gb <= 64  # QOS ia-std: 24 CPU, ~107 GB, 2 jobs
    assert config.run_wait_s < 60  # MCP clients time out around 60 s
    assert config.partition == "ws-ia" and config.background_partition == "gpu"


def test_environment_overrides() -> None:
    config = load_bench_config({
        "SCHUB_BENCH_CPUS": "8", "SCHUB_BENCH_MEM_GB": "32", "SCHUB_BENCH_RUN_WAIT_S": "20",
        "SCHUB_BENCH_BACKGROUND_PARTITION": "ws-ia", "SCHUB_BENCH_IDLE_STOP_MIN": "10",
    })
    assert (config.cpus, config.mem_gb, config.run_wait_s, config.idle_stop_min) == (8, 32, 20.0, 10)
    assert config.background_partition == "ws-ia"


@pytest.mark.parametrize("key,value", [
    ("SCHUB_BENCH_CPUS", "0"), ("SCHUB_BENCH_CPUS", "many"), ("SCHUB_BENCH_RUN_WAIT_S", "120"),
    ("SCHUB_BENCH_HOURS", "25"), ("SCHUB_BENCH_OUTPUT_CHARS", "10"),
])
def test_bad_values_are_refused(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=key):
        load_bench_config({key: value})


def test_settings_carry_bench_config_and_legacy_flag(tmp_path: Path) -> None:
    settings = load_settings({"SCHUB_ROOT": str(tmp_path), "SCHUB_BENCH_CPUS": "6", "SCHUB_LEGACY_TOOLS": "1"})
    assert settings.bench.cpus == 6
    assert settings.legacy_tools is True
    assert settings.bench_dir == tmp_path / "bench"
    assert load_settings({"SCHUB_ROOT": str(tmp_path)}).legacy_tools is False


def test_each_sc_hub_folder_of_an_account_has_its_own_job_names(tmp_path: Path, monkeypatch) -> None:
    """Jobs are found by name: a second folder (the owner's test root) must not stop or adopt the first's."""
    from schub import config

    usual = tmp_path / "schub"
    usual.mkdir()
    (tmp_path / "link").symlink_to(usual)
    monkeypatch.setattr(config, "_default_root", lambda env: usual)
    assert load_settings({"SCHUB_ROOT": str(usual)}).job_prefix == "schub"
    assert load_settings({"SCHUB_ROOT": str(tmp_path / "link")}).job_prefix == "schub"  # ~/schub is a link
    other = load_settings({"SCHUB_ROOT": str(tmp_path / "test-root")}).job_prefix
    assert other.startswith("schub-") and other == load_settings({"SCHUB_ROOT": str(tmp_path / "test-root")}).job_prefix
    assert load_settings({"SCHUB_ROOT": str(tmp_path / "x"), "SCHUB_JOB_PREFIX": "sbt"}).job_prefix == "sbt"
    with pytest.raises(ValueError, match="SCHUB_JOB_PREFIX"):
        load_settings({"SCHUB_ROOT": str(tmp_path / "x"), "SCHUB_JOB_PREFIX": "a b"})
