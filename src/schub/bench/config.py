"""Settings of the bench, from SCHUB_BENCH_* environment variables.

The defaults follow the MBZUAI student cluster: QOS ia-std on ws-ia allows two
running jobs, 24 CPUs and ~107 GB per user, so the workbench stays small enough
to leave room for one batch job. Background jobs (watchdog, twin builds) go to
the gpu partition without a GPU, which has its own budget (16 CPU, 90 GB, 8 h
per job); `background_fallback` is used where that is not allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Mapping

PREFIX = "SCHUB_BENCH_"


@dataclass(frozen=True)
class BenchConfig:
    cpus: int = 12
    mem_gb: int = 48
    hours: int = 8  # wall clock of one workbench job; a successor takes over
    idle_stop_min: int = 30
    run_wait_s: float = 35.0  # the longest a tool call waits before answering "running"
    poll_s: float = 0.5
    output_chars: int = 6000  # text kept per cell (head + tail)
    retire_before_end_s: int = 1800  # USR1 this long before the time limit
    snapshot_max_files: int = 5000
    snapshot_hash_max_mb: int = 64
    partition: str = "ws-ia"
    background_partition: str = "gpu"
    background_fallback: str = "ws-ia"
    watchdog_every_min: int = 30
    dormant_after_h: int = 12  # the watchdog stops re-arming itself after this long without work
    max_running_jobs: int = 2  # per-user running jobs on a capped partition when the QOS cannot be read
    capped_partitions: str = "ws-ia"  # comma-separated; MBZUAI's gpu QOS caps CPU, memory and GPUs, not jobs

    def __post_init__(self) -> None:
        _check_range("CPUS", self.cpus, 1, 24)
        _check_range("MEM_GB", self.mem_gb, 4, 100)
        _check_range("HOURS", self.hours, 1, 24)
        _check_range("IDLE_STOP_MIN", self.idle_stop_min, 1, 24 * 60)
        _check_range("RUN_WAIT_S", self.run_wait_s, 1, 55)
        _check_range("POLL_S", self.poll_s, 0.05, 10)
        _check_range("OUTPUT_CHARS", self.output_chars, 500, 200_000)
        _check_range("RETIRE_BEFORE_END_S", self.retire_before_end_s, 60, 6 * 3600)
        _check_range("SNAPSHOT_MAX_FILES", self.snapshot_max_files, 10, 1_000_000)
        _check_range("SNAPSHOT_HASH_MAX_MB", self.snapshot_hash_max_mb, 0, 100_000)
        _check_range("WATCHDOG_EVERY_MIN", self.watchdog_every_min, 5, 24 * 60)
        _check_range("DORMANT_AFTER_H", self.dormant_after_h, 1, 24 * 30)
        _check_range("MAX_RUNNING_JOBS", self.max_running_jobs, 1, 1000)
        for name in ("partition", "background_partition", "background_fallback"):
            if not _is_partition(getattr(self, name)):
                raise ValueError(f"{PREFIX}{name.upper()} must be a partition name, got {getattr(self, name)!r}")
        if not all(map(_is_partition, self.capped)):
            raise ValueError(f"{PREFIX}CAPPED_PARTITIONS must be partition names, got {self.capped_partitions!r}")

    @property
    def capped(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.capped_partitions.split(",") if p.strip())


def _check_range(key: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ValueError(f"{PREFIX}{key} must be between {low} and {high}, got {value}")


def _is_partition(name: str) -> bool:
    return name.replace("-", "").replace("_", "").isalnum()


def _parse(key: str, raw: str, kind: type) -> object:
    if kind is str:
        return raw
    try:
        return kind(raw)
    except ValueError as exc:
        raise ValueError(f"{PREFIX}{key} must be a number, got {raw!r}") from exc


def load_bench_config(env: Mapping[str, str]) -> BenchConfig:
    values: dict[str, object] = {}
    for spec in fields(BenchConfig):
        key = spec.name.upper()
        raw = env.get(PREFIX + key)
        if raw is None or raw == "":
            continue
        kind = type(spec.default)
        values[spec.name] = _parse(key, raw, kind)
    return BenchConfig(**values)  # type: ignore[arg-type]
