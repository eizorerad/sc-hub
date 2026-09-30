"""guide.html: the cluster for someone who has never used Slurm, next to the dashboard and linked from where the
dashboard talks about the cluster (the cluster overview, the account menu)."""

from __future__ import annotations

import os
from pathlib import Path

from schub.dashboard import build_dashboard
from schub.dashboard.guide import FILE, budgets, render_guide
from schub.dashboard.page import render_page
from schub.dashboard.script import SCRIPT
from schub.dashboard.views_cluster import render_cluster
from schub.overview import Limit, Overview, QosLimits, parse_user_qos
from schub.service import Hub
from schub.slurm import Slurm

# `scontrol show assoc_mgr flags=qos` on the cluster (2026-09-30), the student's own lines
IA_STD = """QOS=ia-std(20)
    User Limits
      test.user(40401)
        MaxJobsPU=2(1) MaxJobsAccruePU=N(0) MaxSubmitJobsPU=N(1)
        MaxTRESPU=cpu=24(8),mem=110000(40960),energy=N(0),node=N(0),billing=N(0),gres/gpu=N(0)
"""
GPU_1 = """QOS=gpu-1(62)
    User Limits
      test.user(40401)
        MaxJobsPU=N(0) MaxJobsAccruePU=N(0) MaxSubmitJobsPU=N(0)
        MaxTRESPU=cpu=16(0),mem=92160(0),energy=N(0),node=N(0),billing=N(0),gres/gpu=1(0)
"""


def overview(**extra) -> Overview:
    limits = (QosLimits(qos="ia-std", partitions=("ws-ia",), limits=parse_user_qos(IA_STD, "test.user")),
              QosLimits(qos="gpu-1", partitions=("gpu",), limits=parse_user_qos(GPU_1, "test.user")))
    return Overview(**{"user": "test.user", "login_node": "lo-02", "generated_at": "2026-09-30 17:00 UTC",
                       "limits": limits, **extra})


def test_the_guide_explains_the_cluster_to_a_newcomer() -> None:
    page = render_guide(None)
    for part in ("Login nodes: the front door", "Never run analyses, training or large downloads on a login node",
                 "Jobs, partitions and the queue", "ws-ia (the default)", "RTX 5000 Ada", "--gres=gpu:1", "cu128",
                 "Storage: the same files on every node", "/l/users/&lt;login&gt;", "3 TB", "NFS", "Lustre",
                 "Nothing has to be copied to a job", "#SBATCH --time=02:00:00", "squeue --me", "scancel",
                 "srun --cpus-per-task=4", "sacct</code> does not work", "Where sc-hub fits"):
        assert part in page, part
    assert 'href="index.html"' in page and 'href="index.html#cluster"' in page  # back to the dashboard
    assert "2 running jobs · 24 CPUs · 107 GB" in page and "1 GPU · 16 CPUs · 90 GB" in page  # measured defaults
    assert "24 hours" in page and "8 hours" in page and "<script" not in page


def test_a_students_own_limits_come_from_the_cluster_when_it_says_them() -> None:
    assert budgets(overview()) == {"ws-ia": "2 running jobs · 24 CPUs · 107 GB", "gpu": "1 GPU · 16 CPUs · 90 GB"}
    changed = Overview(user="u", login_node="lo-01", generated_at="x", limits=(
        QosLimits(qos="ia-std", partitions=("ws-ia",), limits=(Limit(name="running jobs", used=0, limit=3),
                                                               Limit(name="CPUs", used=0, limit=32),
                                                               Limit(name="memory (GB)", used=0, limit=None))),))
    assert budgets(changed) == {"ws-ia": "3 running jobs · 32 CPUs", "gpu": "1 GPU · 16 CPUs · 90 GB"}
    page = render_guide(Overview(user="a<b", login_node="lo-01", generated_at="x"))
    assert "a&lt;b" in page and "a<b" not in page


def test_the_guide_does_not_change_with_what_is_in_use_or_the_time() -> None:
    """The laptop's copy fetches a changed file again: the guide must only change when the cluster's rules do."""
    busy = overview(generated_at="2026-09-30 18:00 UTC")
    quiet = overview(generated_at="2026-09-30 17:00 UTC")
    assert render_guide(busy) == render_guide(quiet)


def test_the_dashboard_links_the_guide_where_it_talks_about_the_cluster(settings, cluster) -> None:
    assert 'href="guide.html"' in render_cluster(overview()) and 'href="guide.html"' in render_cluster(None)
    info = build_dashboard(Hub(settings, Slurm(cluster)))
    index = Path(info.path).read_text()
    assert '<a href="guide.html"><b>Cluster guide</b>' in index
    # the setup's welcome lives on the dashboard's local server: its menu entry shows only when served from there
    assert '<a href="welcome" data-served hidden><b>Getting started</b>' in index
    assert "location.protocol.startsWith('http')" in SCRIPT and "[data-served]" in SCRIPT
    guide = settings.view_dir / FILE
    assert guide.is_file() and "The MBZUAI student cluster in five minutes" in guide.read_text()
    stamp = guide.stat().st_mtime_ns
    os.utime(guide, ns=(stamp - 10**9, stamp - 10**9))
    build_dashboard(Hub(settings, Slurm(cluster)))
    assert guide.stat().st_mtime_ns == stamp - 10**9  # unchanged text: the file is left as it was


def test_the_account_menu_keeps_the_guide_one_line() -> None:
    from schub.dashboard.page import _account  # noqa: PLC2701 - the menu itself

    assert render_page is not None
    menu = _account(type("Snap", (), {"overview": None, "user": "test.user", "generated_at": "2026-09-30 17:00 UTC"})())
    guide = menu.split('href="guide.html">', 1)[1].split("</a>", 1)[0]
    assert guide.count("<b>") == 1 and "five minutes" in guide


def test_the_dashboard_runs_only_its_own_two_scripts(settings, cluster) -> None:
    """Served over http, a script injected through data could read every project: the page pins its own two inline
    scripts by hash, so nothing else inline runs (served by the laptop's server or opened as a file)."""
    import base64
    import hashlib
    import re

    index = Path(build_dashboard(Hub(settings, Slurm(cluster))).path).read_text()
    policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', index)[1]
    inline = re.findall(r"<script>(.*?)</script>", index, re.S)
    assert len(inline) == 2 and "<script " not in index  # (and no script with attributes)
    for code in inline:
        assert f"'sha256-{base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()}'" in policy
    assert "'unsafe-inline'" not in policy and "object-src 'none'" in policy and "script-src 'self' file:" in policy
    assert index.index("Content-Security-Policy") < index.index("<script>")
