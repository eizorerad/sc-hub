from __future__ import annotations

from schub.overview import (
    collect_overview,
    parse_df,
    parse_jobs,
    parse_lfs_quota,
    parse_partitions,
    parse_sinfo_cpus,
    parse_sinfo_gpus,
    parse_user_qos,
    size_gb,
)

# Real output captured on the MBZUAI student cluster (2026-09-23).
LFS = """Disk quotas for usr leonid.klarov (uid 3525):
     Filesystem    used  bquota  blimit  bgrace   files  iquota  ilimit  igrace
             /l  2.021T      3T   3.15T       - 2416558 50000000 52500000       -
"""
DF = """Filesystem         1024-blocks         Used    Available Capacity Mounted on
nfs-head:/c/home 517550050304 217974972416 300575077888      43% /home
"""
QOS = """    User Limits
      someone.else(2955)
        MaxJobsPU=2(0) MaxJobsAccruePU=N(0) MaxSubmitJobsPU=N(2)
        MaxTRESPU=cpu=24(0),mem=110000(0),energy=N(0),gres/gpu=N(0)
      leonid.klarov(3525)
        MaxJobsPU=2(1) MaxJobsAccruePU=N(0) MaxSubmitJobsPU=N(1)
        MaxTRESPU=cpu=24(8),mem=110000(32768),energy=N(0),node=N(1),billing=N(8),fs/disk=N(0),vmem=N(0),pages=N(0),gres/gpu=N(0),gres/gpumem=N(0),gres/gpuutil=N(0)
        MaxTRESRunMinsPU=cpu=N(4387),mem=N(17969971)
"""
PARTITIONS = (
    "PartitionName=ws-ia AllowGroups=ALL Default=YES QoS=ia-std DefaultTime=1-00:00:00 MaxNodes=1 MaxTime=1-00:00:00\n"
    "PartitionName=gpu AllowGroups=ALL Default=NO QoS=gpu-1 DefaultTime=NONE MaxNodes=1 MaxTime=UNLIMITED\n"
)
SINFO_CPUS = "ws-ia*|374/5098/96/5568|230000|116\ngpu|34/478/0/512|772633|4\n"
SINFO_GPUS = (
    "gpu                           gpu:nvidia-rtx-5000-ada-generation:8                        gpu:nvidia-rtx-5000-ada-generation:2(IDX:0-1)\n"
    "gpu                           gpu:nvidia-rtx-5000-ada-generation:8                        gpu:nvidia-rtx-5000-ada-generation:0(IDX:N/A)\n"
    "ws-ia*                        gpu:nvidia-rtx-5000-ada-generation:1                        gpu:nvidia-rtx-5000-ada-generation:1(IDX:0)\n"
    "ws-ia*                        gpu:nvidia-rtx-5000-ada-generation:1                        gpu:nvidia-rtx-5000-ada-generation:0(IDX:N/A)\n"
)
SQUEUE = (
    "205879|vcc-court|PENDING|gpu|2|8G|N/A|0:00|45:00||BeginTime\n"
    "205680|personal-ws|RUNNING|ws-ia|8|32G|gres/gpu:1|14:10:02|1-00:00:00|ws-l1-004|None\n"
)


def test_sizes():
    assert size_gb("2.021T") == 2069.5 and size_gb("3T") == 3072 and size_gb("110000") == 107.42
    assert size_gb("8G") == 8 and size_gb("-") is None


def test_storage_parsers():
    lustre = parse_lfs_quota(LFS)
    assert lustre.used_gb == 2069.5 and lustre.limit_gb == 3072 and lustre.files == 2416558 and lustre.files_limit == 50000000
    home = parse_df(DF, "Home (NFS)")
    assert home.path == "/home" and 200_000 < home.used_gb < 210_000 and "no per-user quota" in home.note


def test_user_limits_are_found_for_this_user_only():
    limits = {limit.name: (limit.used, limit.limit) for limit in parse_user_qos(QOS, "leonid.klarov")}
    assert limits["running jobs"] == (1, 2) and limits["CPUs"] == (8, 24)
    assert limits["memory (GB)"] == (32.0, 107.4) and limits["GPUs"] == (0, None)
    assert parse_user_qos(QOS, "nobody") == ()


def test_cluster_load_and_jobs():
    partitions = parse_partitions(PARTITIONS)
    assert partitions["ws-ia"]["QoS"] == "ia-std" and partitions["gpu"]["MaxTime"] == "UNLIMITED"
    assert parse_sinfo_cpus(SINFO_CPUS)["ws-ia"] == (374, 5568, 224.6, 116)
    assert parse_sinfo_cpus("mixed|1/2/0/3|230000+|2\nbroken|x\n") == {"mixed": (1, 3, 224.6, 2)}
    assert parse_sinfo_gpus(SINFO_GPUS) == {"gpu": (2, 16), "ws-ia": (1, 2)}
    jobs = parse_jobs(SQUEUE)
    assert jobs[1].gpus == 1 and jobs[1].mem_gb == 32 and jobs[1].node == "ws-l1-004" and jobs[0].gpus == 0


def test_collect_overview_survives_failing_commands(settings, monkeypatch):
    monkeypatch.setenv("USER", "leonid.klarov")
    outputs = {
        "lfs": LFS, "df": DF, "who": "leonid.klarov pts/40 2026-09-22 23:39 (zap-2)\nother pts/1 x\n",
        "sinfo": SINFO_CPUS, "squeue": SQUEUE, "du": "2048\t/x\n",
    }

    def fake(args):
        if args[0] == "scontrol" and args[2] == "partition":
            return PARTITIONS
        if args[0] == "scontrol":
            if "qos=gpu-1" in args:
                raise OSError("assoc_mgr unavailable")
            return QOS
        if args[0] == "sinfo" and "-N" in args:
            return SINFO_GPUS
        return outputs[args[0]]

    (settings.root / "data").mkdir(exist_ok=True)
    overview = collect_overview(settings, fake)
    assert [s.name for s in overview.storage] == ["Lustre (/l)", "Home (NFS)"]
    assert overview.limits[0].qos == "ia-std" and overview.limits[0].partitions == ("ws-ia",)
    assert any("gpu-1" in p for p in overview.problems)
    assert overview.logins == ("pts/40 2026-09-22 23:39 (zap-2)",)
    assert overview.footprint == {} and len(overview.jobs) == 2  # du runs detached; first build has no sizes
