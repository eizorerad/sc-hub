---
name: slurm_jobs
description: Send heavy or long work to Slurm with %%slurm (GPU training, big data, hours); how results come back.
---
# Heavy work as Slurm jobs: `%%slurm`

The kernel is for looking and trying. Anything that needs a GPU, more than a few
minutes, or much memory goes to its own Slurm job:

```
%%slurm --gpus 1 --cpus 8 --mem 64G --time 6h
import anndata as ad, scvi
adata = ad.read_h5ad("../data/k562_essential.h5ad")
...
adata.write_h5ad("k562_scvi.h5ad")
```

- **Self-contained.** The job runs in a new process: it reads and writes files, not
  kernel variables. The answer warns about names the cell uses that only exist in
  the kernel. It starts in the project's `work/` folder, and `bench` is there as in
  the kernel (`bench.data_dir()`, `bench.fetch`, `bench.twin`), except with `--python`.
- **Frozen.** The cell's code is copied with a checksum when you send it; the job
  refuses to run if the copy changed. To change it, send a new cell.
- **It does not block.** The cell returns at once with the job id. The same journal
  entry gets the job's state, exit code, the files it wrote, downloads and check
  results when it ends. `wait(ref)` waits for the job as well (about half a minute
  per call; call it again while it runs), `journal(project)` shows it, and the log is
  in `jobs/<cell>-<key>/slurm-<id>.log` (read it with `files`). Do not poll squeue
  from a cell.
- **Limits** (per job): ws-ia up to 24 h, 24 CPU, 100 GB; gpu partition up to 8 h,
  16 CPU, 90 GB; one GPU per student (`--gpus 1`). Longer training: save checkpoints
  and continue in a new job.
- **Slots.** A ws-ia job needs a free slot (two per student, the workbench takes
  one). If it waits (`QOSMaxJobsPerUserLimit`), tell the student; `stop("workbench")`
  frees the workbench's slot when you are only waiting for jobs.
- **Checks.** `run(..., checks=[...])` on a %%slurm cell runs the checks when the
  job ends, on what it wrote (`skills("checks")`).
- **Cancel** with `stop("<job id>")`; only jobs the bench sent can be cancelled.
- A job killed by Slurm (time limit, out of memory) is recorded in its cell as
  TIMEOUT / OUT_OF_MEMORY by the watchdog.
- While a job runs, files it writes in `work/` also show up in the file list of a
  kernel cell that runs at the same time; the job's own entry lists them correctly.
- `--bash` runs the cell as a shell script; `--python /path/to/venv/bin/python`
  runs it with another environment (e.g. a paper's own).
