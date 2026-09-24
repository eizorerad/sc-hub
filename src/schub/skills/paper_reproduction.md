---
name: paper_reproduction
description: Reproduce a paper's result: target table, feasibility on our GPUs, the student's choice of scale, the code at a pinned commit, checkpointed runs, our numbers next to the paper's.
---
# Reproducing a paper

## 1. Targets before code

Read the paper (and its supplement) and write a table, as a note, before any run:

| claim | number | data and split | metric definition | paper's hardware and time |
|---|---|---|---|---|

Quote where each number is (Table 2, Fig. 3b). A metric without its exact definition
(which genes, which cells, which split) cannot be compared: say so.

## 2. Feasibility here

What we have: 1 RTX 5000 Ada (32 GB) per student, gpu jobs up to 8 h, ws-ia CPU jobs
up to 24 h, 3 TB of storage, internet on the nodes (skills("mbzuai_slurm")).
Estimate for each target: parameters and GPU memory for training (weights, gradients
and Adam: about 16 bytes per parameter, plus activations), cells or tokens times
epochs, hours on one GPU. Then sort the targets:

- **full**: fits as published;
- **reduced**: fewer cells, epochs or a smaller model; say what changes and why the
  claim can still be tested;
- **released weights**: evaluation only, with the authors' checkpoint;
- **not feasible here**.

Show this to the student and let them choose. Do not start hours of GPU work
without that choice.

## 3. Registration

A `note(kind="registration")` with what will run, what counts as reproduced (the
tolerance per metric) and what would count against it. Write it before the runs.

## 4. The code and its environment

```
repo = bench.clone("https://github.com/<lab>/<model>", ref="<the tag or commit the paper cites>")
python = bench.repo_env(repo, python="3.10", torch="2.1.2", cuda="cu121", requirements="requirements.txt")
```

- The journal records the commit. Patches to the repository: write them as a file in
  `work/` and apply them in a cell, so they are in the journal too.
- torch ≥ 2.7 needs cu126 or cu128; torch 2.0–2.3 has cu118 and cu121 builds. The
  nodes run CUDA ≤ 12.8, so cu13x never works.
- First a smoke run on a twin or a small subset: `%%slurm --gpus 1 --time 20m --python <python>`.

## 5. Long runs

```
%%slurm --gpus 1 --time 8h --python <python>
from schub_ckpt import Run
run = Run("runs/full", registration={"config": cfg, "data": data_sha256, "code": commit})
resumed = run.start()
...
with run.signals():
    while step < total and not run.stop_requested:
        ...
        if step % 1000 == 0:
            run.save(step, lambda d: torch.save(state, d / "state.pt"), loss=loss)
run.save(step, lambda d: torch.save(state, d / "state.pt"), status="done" if step >= total else "paused")
```

The job hears SIGUSR1 minutes before its time limit; `run.signals()` turns it into
`stop_requested`, so the last checkpoint is saved. Longer than 8 h: send the same
cell again, and it resumes from the latest checkpoint (the registration must not
change; a new one needs a new run folder).

## 6. Our numbers next to the paper's

```
bench.compare(ours={"pearson_delta": 0.41}, paper={"pearson_delta": 0.45},
              source="Table 2, scGPT row", tolerance=0.1, name="table2")
# or the interval the registration named: bounds={"pearson_delta": (0.38, 0.52)}
```

Then a finding per target citing the compare cell, and a `verdict` note:
reproduced, partly, or not, with `because` and `reverses_if`. Name the likely causes
of a gap (seed, split, preprocessing, metric definition, reduced scale) instead of
tuning until the numbers match.
