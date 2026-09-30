"""guide.html: the MBZUAI student cluster for someone who has never used Slurm (login nodes, jobs and
partitions, GPUs, storage, a first job), next to the dashboard and linked from its cluster overview.

A page of its own, written only when its text changes: the laptop's copy of the dashboard then fetches it
once, not every minute. The hardware and the rules were measured on the cluster (2026-09-30); a student's
own limits come from the overview when it could read them, so a change by the HPC team shows up here.
"""

from __future__ import annotations

from ..overview import Overview, QosLimits
from .html import esc
from .style import CSS

FILE = "guide.html"
CHECKED = "September 2026"
# Per partition: (its QOS, what a student may use at once when the overview cannot say, the longest job).
STATIC = {"ws-ia": ("ia-std", "2 running jobs · 24 CPUs · 107 GB", "24 hours"),
          "gpu": ("gpu-1", "1 GPU · 16 CPUs · 90 GB", "8 hours")}

GUIDE_CSS = """
.guide{max-width:780px;margin:0 auto;padding:18px 20px 64px;font-size:15px;line-height:1.65}
.guide h1{font-size:26px;margin:18px 0 6px}.guide h2{font-size:18px;margin:34px 0 10px}
.guide p,.guide ul{margin:8px 0}.guide li{margin:4px 0}.guide .lead{color:var(--muted);font-size:16px}
.guide table{display:table;margin:10px 0;font-size:14px}.guide th,.guide td{text-align:left;vertical-align:top;
padding:8px 10px;border-bottom:1px solid var(--line)}.guide th{font-weight:600}.guide td:first-child{color:var(--muted)}
.guide pre{background:var(--soft);padding:12px 14px;border-radius:10px;overflow-x:auto;white-space:pre;font-size:13px}
.guide code{background:var(--soft);padding:1px 5px;border-radius:5px;font-size:13px}.guide pre code{background:none;padding:0}
.guide .rule{background:var(--badbg);color:var(--bad);border-radius:12px;padding:10px 14px;margin:12px 0}
.guide .back{font-size:13.5px}.guide .foot{color:var(--muted);font-size:13px;margin-top:36px}
.map{display:grid;grid-template-columns:1fr auto 1.2fr auto 1.5fr;gap:8px;align-items:center;margin:14px 0 4px}
.map .box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;font-size:13.5px;
line-height:1.45}.map .box b{display:block;font-size:14.5px}.map .arrow{color:var(--muted);font-size:12px;text-align:center}
.map .arrow::after{content:"→";display:block;font-size:20px;line-height:1}
.map .disk{grid-column:3 / 6;background:var(--soft);border-radius:12px;padding:8px 12px;font-size:13.5px;text-align:center}
@media (max-width:640px){.map{grid-template-columns:1fr}.map .arrow::after{content:"↓"}.map .disk{grid-column:1}
.guide table{display:block;overflow-x:auto}}
"""


def _amount(limit: float) -> str:
    return f"{limit:g}" if limit < 100 else f"{round(limit)}"


def _budget(group: QosLimits) -> str:
    """What a student may use at once in one QOS, as the cluster says it now ('2 running jobs · 24 CPUs · 107 GB')."""
    words = {"running jobs": ("running job", "running jobs"), "CPUs": ("CPU", "CPUs"), "GPUs": ("GPU", "GPUs")}
    parts = []
    for limit in sorted(group.limits, key=lambda item: ("running jobs", "GPUs", "CPUs", "memory (GB)").index(item.name)
                        if item.name in ("running jobs", "GPUs", "CPUs", "memory (GB)") else 9):
        if limit.limit is None:
            continue
        if limit.name == "memory (GB)":
            parts.append(f"{round(limit.limit)} GB")
        elif limit.name in words:
            one, many = words[limit.name]
            parts.append(f"{_amount(limit.limit)} {one if limit.limit == 1 else many}")
    return " · ".join(parts)


def budgets(ov: Overview | None) -> dict[str, str]:
    """Partition -> what one student may use there at once: the cluster's own numbers where the overview has them."""
    found = {name: static for name, (_, static, _) in STATIC.items()}
    for group in ov.limits if ov else ():
        text = _budget(group)
        for partition in group.partitions:
            if text and partition in found:
                found[partition] = text
    return found


def render_guide(ov: Overview | None) -> str:
    budget = budgets(ov)
    user = esc(ov.user) if ov and ov.user else "&lt;login&gt;"
    body = f"""
<p class="back"><a href="index.html">← Your dashboard</a></p>
<h1>The MBZUAI student cluster in five minutes</h1>
<p class="lead">A Slurm cluster is many computers shared by many people. You log in to one of them and ask Slurm,
the scheduler, to run your work on the others. This page shows how this cluster is laid out and the few rules that keep
it working for everyone.</p>

<div class="map" role="img" aria-label="Your laptop connects by ssh to a login node, which sends jobs through Slurm to
the compute nodes; the same storage is attached to all of them">
<div class="box"><b>Your laptop</b>browser, terminal, your assistant</div>
<div class="arrow">ssh</div>
<div class="box"><b>Login nodes</b>lo-01, lo-02: edit, install, submit, watch</div>
<div class="arrow">sbatch / srun</div>
<div class="box"><b>Compute nodes</b>ws-ia: 116 workstations, 1 GPU each<br>gpu: 4 servers, 8 GPUs each</div>
<div class="disk">The same storage everywhere: <code>/home</code> (NFS) and <code>/l</code> (Lustre)</div>
</div>

<h2>Login nodes: the front door</h2>
<ul>
<li><code>ssh {user}@login-student-lab.mbzu.ae</code> lands on a login node (lo-02, or lo-01). About two thousand
students share them.</li>
<li>Use them for light work: editing files, git, installing packages, submitting and watching jobs, and Codex or
Claude Code.</li>
</ul>
<p class="rule"><b>Never run analyses, training or large downloads on a login node.</b> Each person gets three CPU cores
there, so it is slow for you and for everyone else. Put the work in a job.</p>

<h2>Jobs, partitions and the queue</h2>
<ul>
<li>A <b>job</b> is work plus a request: how many CPUs, how much memory, how many GPUs, for how long. Slurm starts it
on a compute node when that much is free; until then it waits in the <b>queue</b>.</li>
<li>A <b>partition</b> is a group of compute nodes with its own rules. This cluster has two:</li>
</ul>
<table>
<tr><th></th><th>ws-ia (the default)</th><th>gpu</th></tr>
<tr><td>Nodes</td><td>116 lab workstations: 48 CPU threads, about 230 GB of memory and 1 GPU each</td>
<td>4 servers: 128 CPU threads, about 770 GB of memory and 8 GPUs each</td></tr>
<tr><td>You, at once</td><td>{esc(budget["ws-ia"])}</td><td>{esc(budget["gpu"])}</td></tr>
<tr><td>Longest job</td><td>{STATIC["ws-ia"][2]}</td><td>{STATIC["gpu"][2]}</td></tr>
<tr><td>Usually</td><td>free: most jobs start at once</td><td>busy: a GPU job may wait</td></tr>
</table>
<ul>
<li>A job beyond your share waits (the reason says so, e.g. <code>QOSMaxJobsPerUserLimit</code>) and starts when one
of yours ends. The two partitions have separate shares.</li>
<li>A running job is never interrupted for someone else; it ends when it is done or at its time limit. Someone who used
a lot lately waits a little longer (fair share).</li>
<li>Ask for memory: without <code>--mem</code> a job gets 1 GB per CPU.</li>
</ul>

<h2>GPUs</h2>
<ul>
<li>All of them are NVIDIA RTX 5000 Ada with 32 GB of memory. Ask for one with <code>--gres=gpu:1</code>.</li>
<li>The driver supports CUDA up to 12.8: install PyTorch from its cu128 wheels
(<code>pip install torch --index-url https://download.pytorch.org/whl/cu128</code>). The default wheels install but see
no GPU here.</li>
<li>Leave <code>CUDA_VISIBLE_DEVICES</code> alone: Slurm sets it to the GPU your job got.</li>
</ul>

<h2>Storage: the same files on every node</h2>
<table>
<tr><th>Where</th><th>What it is</th><th>Size and limits</th><th>For</th></tr>
<tr><td><code>/home/{user}</code></td><td>NFS, shared</td><td>482 TB for everyone; no per-person limit shows</td>
<td>settings, code, small files</td></tr>
<tr><td><code>/l/users/{user}</code></td><td>Lustre, fast and parallel</td><td>492 TB; your quota is 3 TB and 50 million
files</td><td>data, results, environments; sc-hub lives in <code>/l/users/{user}/schub</code></td></tr>
<tr><td><code>/tmp</code> on a compute node</td><td>the node's own disk</td><td>up to 1.8 TB, only while your job runs</td>
<td>fast scratch; deleted when the job ends</td></tr>
</table>
<ul>
<li><code>/home</code> and <code>/l</code> are attached to the login nodes and to every compute node: a job sees exactly the
files you see when you log in. Nothing has to be copied to a job.</li>
<li>Your quota: <code>lfs quota -h -u $USER /l</code>. Heavy reading and writing belongs in jobs. Lustre now and then
fails a write with EFAULT or EIO; the same write usually works when retried.</li>
<li>The compute nodes have internet (PyPI, Hugging Face, GEO), so large downloads can run in a job too.</li>
</ul>

<h2>Your first job</h2>
<p>A batch job is a script with its request at the top. Save it as <code>hello.sh</code> and submit it with
<code>sbatch hello.sh</code>:</p>
<pre><code>#!/bin/bash
#SBATCH --job-name=hello
#SBATCH --partition=ws-ia
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=%x-%j.out
python my_analysis.py</code></pre>
<table>
<tr><td><code>squeue --me</code></td><td>your jobs: PD waits (and why), R runs</td></tr>
<tr><td><code>scontrol show job 12345</code></td><td>everything about one job</td></tr>
<tr><td><code>scancel 12345</code></td><td>stop a job of yours</td></tr>
<tr><td><code>srun --cpus-per-task=4 --mem=16G --time=01:00:00 --pty bash</code></td><td>a shell on a compute node
(<code>exit</code> ends the job)</td></tr>
<tr><td><code>--partition=gpu --gres=gpu:1 --time=08:00:00</code></td><td>add these for a GPU on the gpu partition</td></tr>
</table>
<p><code>sacct</code> does not work on this cluster's login nodes; <code>squeue</code> and <code>scontrol show job</code>
do.</p>

<h2>Where sc-hub fits</h2>
<ul>
<li>You rarely need any of this by hand. sc-hub's workbench, your live Python, is one of your two ws-ia jobs, and
heavy steps go out as Slurm jobs of their own. Your assistant picks the partition and the request, and tells you when
something waits for a slot.</li>
<li>When you are done for the day, ask your assistant to stop the workbench: that frees its slot.</li>
<li>Your limits and jobs right now: <a href="index.html#cluster">Cluster overview</a>.</li>
</ul>
<p class="foot">Measured on the cluster in {CHECKED}. Your own limits above come from the cluster itself when sc-hub
could read them. Questions about the cluster go to MBZUAI's HPC team.</p>
"""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">'
            f"<title>The cluster in five minutes · sc-hub</title><style>{CSS}{GUIDE_CSS}</style></head>"
            f'<body><main class="guide">{body}</main></body></html>')
