from __future__ import annotations

import json
import subprocess
from typing import Sequence

from schub.bench.slots import slot_usage
from schub.config import Settings
from schub.slurm import Slurm

ROWS = ("1|personal-ws|RUNNING|3:00|None|ws-ia|1-00:00:00\n"
        "2|vcc-census|RUNNING|0:10|None|ws-ia|4:00:00\n"
        "3|schub-bench-workbench|PENDING|0:00|(QOSMaxJobsPerUserLimit)|ws-ia|8:00:00\n"
        "4|vcc-watch|PENDING|0:00|(BeginTime)|gpu|6:00\n")


def fake(rows: str, code: int = 0):
    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), code, rows, "boom" if code else "")
    return run


def test_counts_running_jobs_on_the_workbench_partition(settings: Settings) -> None:
    usage = slot_usage(settings, Slurm(runner=fake(ROWS)))
    assert (usage.partition, usage.running, usage.limit, usage.full) == ("ws-ia", 2, 2, True)
    assert usage.summary() == ("ws-ia 2/2 running FULL: personal-ws RUNNING, vcc-census RUNNING, "
                               "schub-bench-workbench PENDING (QOSMaxJobsPerUserLimit)")


def test_limit_comes_from_the_cached_overview(settings: Settings) -> None:
    settings.cache_dir.mkdir(parents=True)
    (settings.cache_dir / "overview.json").write_text(json.dumps({"limits": [
        {"qos": "ia-std", "partitions": ["ws-ia"], "limits": [{"name": "running jobs", "used": 2, "limit": 4}]}]}))
    usage = slot_usage(settings, Slurm(runner=fake(ROWS)))
    assert usage.limit == 4 and not usage.full


def test_squeue_failure_is_reported_as_unknown(settings: Settings) -> None:
    usage = slot_usage(settings, Slurm(runner=fake("", code=1)))
    assert usage.unknown and "unknown" in usage.summary()
