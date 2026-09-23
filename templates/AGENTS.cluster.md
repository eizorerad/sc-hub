# sc-hub on the cluster

This folder (`$SCHUB_ROOT`, default `/l/users/$USER/schub`, linked as `~/schub`)
is a student's sc-hub workspace. Layout:

| Path | Contents |
|---|---|
| `bin/` | `schub` (CLI), `schub-mcp` (MCP server on stdio) |
| `projects/<p>[/<sub>]/` | `project.yaml`, `pipelines/<branch>.yaml`, `ideas/<slug>.md`, `logbook.md`, `runs/` links |
| `data/<name>/` | The student's own datasets: `data.h5ad` + `dataset.yaml`, FASTQ reads + `fastq.yaml` (`schub register-fastq`), imported Seurat objects, or loose `.h5ad` files |
| `library-local/` | Datasets and models downloaded because the shared library lacked them or was unreadable |
| `runs/<run_id>/` | `manifest.json`, `plan.json`, `NN_<brick>` links to step folders |
| `cache/steps/<key>/` | Content-addressed step outputs, logs, results; shared by all branches |
| `plans/` | Validated plans |
| `view/` | Static dashboard (mirrored to the laptop by `schub-view`) |
| `sessions/<job>/` | Interactive sessions (JupyterLab, cellxgene); private, holds the session token |
| `notebooks/` | Run notebooks (`schub notebook <run>`: each step's exact code and parameters); `work/` holds what they re-run |
| `logs/` | Audit log of tool calls; `fetch/` download job logs |
| `trash/` | Move things here instead of deleting |

The shared library (`$SCHUB_LIBRARY`, read-only) provides the environment,
datasets (count matrices and FASTQ), models, kallisto indices and tools
(cellxgene, R + Seurat, optionally Cell Ranger). Nothing is ever written there
by students.

## Rules for agents working here

- Work inside a project. A variant of an analysis is a branch
  (`schub branch-save <project> <name> '{"from": "main", "overrides": {...}}'`),
  not a copied folder. Record hypotheses as ideas, decisions in the logbook.
- Use `~/schub/bin/schub` (same operations as the MCP tools).
- Never run heavy computation on the login node; everything goes through Slurm.
  Downloads too: `schub fetch` runs inside a job (`fetch_asset` queues one).
- Do not edit `cache/steps` or `runs` by hand: they are the provenance record.
- A new analysis type becomes a new brick (spec + impl + tests), not an ad-hoc script.
- Never read `sessions/*/connection.json` or run `schub session-info`: they hold the
  session token, which must not reach the chat. Only `schub-lab` on the laptop uses them.
