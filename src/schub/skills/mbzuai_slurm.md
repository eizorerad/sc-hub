---
name: mbzuai_slurm
description: The MBZUAI student cluster as it really behaves: partitions, job slots, GPUs, time limits, storage, CUDA.
---
# The MBZUAI student cluster

Where your cells run and what limits them (measured 2026-09).

| | ws-ia (default) | gpu |
|---|---|---|
| Nodes | 116 × 48 CPU, ~230 GB, 1 RTX 5000 Ada (32 GB) | 4 × 128 CPU, ~770 GB, 8 RTX 5000 Ada |
| Per student | **2 running jobs**, 24 CPU, ~107 GB | 16 CPU, 90 GB, **1 GPU** |
| Per job | 24 h | 8 h |

- The workbench (your live kernel) is one of the two ws-ia jobs; an interactive
  `personal-ws` job is another. A heavy cell sent with `%%slurm` needs a free
  slot; if both are taken it waits (`QOSMaxJobsPerUserLimit`). Every answer shows
  the slots; say so to the student instead of retrying.
- The workbench stops itself after a while without cells, to free its slot. The
  next cell starts it again (a minute or so in the queue). Its variables are then
  gone: keep results in files.
- GPU work: 1 GPU per student, 32 GB of memory. A paper trained on 8×A100 for days
  does not fit; plan a reduced or released-weights reproduction instead.
- The node driver supports CUDA up to 12.8: install torch from the cu118–cu128
  wheels. The default cu130 wheels import fine but silently see no GPU.
- Storage: your folder on Lustre (`/l/users/<you>`, 3 TB quota). Do heavy reading
  and writing inside cells and jobs, never on the login node. Lustre sometimes fails
  a write with EFAULT or EIO; the same write usually works when retried.
- Compute nodes have internet (PyPI, Hugging Face, figshare, GEO).
- `sacct` does not work here; the bench reads job states from squeue and scontrol.
