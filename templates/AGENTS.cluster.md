# sc-hub on the cluster

This folder (`$SCHUB_ROOT`, default `/l/users/$USER/schub`, linked as `~/schub`)
is a student's sc-hub workspace. Layout:

| Path | Contents |
|---|---|
| `bin/` | `schub` (CLI), `schub-mcp` (MCP server on stdio), `schub-gate` (what the sc-hub SSH key may run) |
| `projects/<p>[/<sub>]/` | `project.yaml`, `journal/` (cells, notes, hand-over, checkpoint: the record of the work), `work/` (the kernel's working folder), and from the brick era `pipelines/`, `ideas/`, `logbook.md`, `runs/` |
| `bench/` | The workbench: `inbox/` cell requests, `claimed/<job>/`, `workbench.json` (state and heartbeat), `watchdog.json`, `logs/`, `STOP` (the off switch) |
| `data/<name>/` | The student's own datasets: `data.h5ad` + `dataset.yaml`, FASTQ reads + `fastq.yaml` (`schub register-fastq`), imported Seurat objects, or loose `.h5ad` files |
| `library-local/` | Your own datasets and models: the starter set, and anything a shared library (if one is set) lacks |
| `runs/<run_id>/` | `manifest.json`, `plan.json`, `NN_<brick>` links to step folders |
| `cache/steps/<key>/` | Content-addressed step outputs, logs, results; shared by all branches |
| `plans/` | Validated plans |
| `view/` | Static dashboard (mirrored to the laptop by `schub-view`) |
| `sessions/<job>/` | Interactive sessions (JupyterLab, cellxgene); private, holds the session token |
| `notebooks/` | Run notebooks (`schub notebook <run>`: each step's exact code and parameters); `work/` holds what they re-run |
| `logs/` | Audit log of tool calls; `gate.log` (commands through the sc-hub key); `fetch/` download job logs |
| `queue/` | Plans waiting for a free slot (sc-hub submits them when a pipeline ends); `failed/` those that could not be submitted |
| `trash/` | Move things here instead of deleting |

If `$SCHUB_LIBRARY` is set, that shared library (read-only) provides the
environment, datasets (count matrices and FASTQ), models, kallisto indices and
tools (cellxgene, R + Seurat, optionally Cell Ranger); nothing is ever written
there by students. Without it, everything lives in this folder.

## Rules for agents working here

- Work inside a project, through the bench: `schub bench-run <project> --code ...
  --why ... --expect ...` (the same as the MCP `run` tool). Every cell lands in
  `projects/<p>/journal/`.
- Never run heavy computation on the login node; cells run in the workbench job,
  heavy work in its own Slurm job.
- Do not edit `journal/`, `bench/`, `cache/steps` or `runs` by hand: they are the
  provenance record. A correction is a new note, never an edit. Inputs in `data/`
  and a project's `data/` are never changed either: derive into `work/`.
- `touch bench/STOP` stops the bench (nothing runs, nothing restarts); removing it
  lets the next cell start the workbench again.
- Never read `sessions/*/connection.json` or run `schub session-info`: they hold the
  session token, which must not reach the chat. Only `schub-lab` on the laptop uses them.
