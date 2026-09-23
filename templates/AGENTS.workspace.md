# sc-hub workspace

You are helping a biology graduate student analyse single-cell data on the MBZUAI
Slurm cluster. The `schub` MCP server is the paved road: it runs under the
student's own cluster account, validates a pipeline before anything is queued,
and records what was run.

## Default workflow

1. `list_projects`. Work inside the project the student means; if none fits,
   `create_project` with the scientific question in one sentence.
2. `list_datasets`, then `inspect_dataset`. Confirm the scientific metadata with
   the student before planning: which obs column is the condition, which is the
   biological replicate (donor/sample), which is the technical batch, and the
   reference level. Never infer these silently from column names.
3. `list_recipes` and pick the one that fits (below); fill it with
   `recipe_steps`, then `save_branch` (name the first branch `main`). For a
   variant, save a new branch with `from_branch` + `overrides`; shared steps are
   reused from cache. Show the student the plan, its warnings and GPU-hours. If
   it has errors, explain them; do not bypass them.
4. `submit_plan`, then check `run_status` every few minutes, not in a tight loop.
5. `run_results` for numbers (this also writes the project logbook), then
   `make_dashboard`; the student sees it with `./schub-view` (Windows:
   `.\schub-view.cmd`). Each step there has a cell map (UMAP by any label).
6. Hypotheses go in `add_idea` (with `reverses_if`), decisions in
   `add_logbook_entry`. Link ideas to branches with `update_idea`.

## Changing a pipeline: fix or fork

The student often points at a step from the dashboard, as a reference like
`pbmc1k-fastq/main#4` (every step there has "Fix this step" / "Try an
alternative from here" buttons that copy a request for you).

- `inspect_step("<ref>")` first: brick, params, state, results, log.
- "This step is wrong" -> `revise_branch(project, branch, step, reason, params
  or brick)`: same branch, next revision (r2, r3...), the reason is kept in the
  branch history. Earlier steps come from cache.
- "From here on, try something else" -> `fork_branch(project, branch, step,
  new_branch, reason, params/brick/then)`: a new branch; the original stays.
- Never overwrite a branch to try an alternative. `branch_history` shows the
  revisions with reasons and changes.

## Defaults (the MBZUAI single-cell course: Python, scverse, scvi-tools)

| Situation | Recipe / bricks |
|---|---|
| Count matrix, one batch, first look | `standard_analysis`: qc_filter, normalize_embed, annotate_celltypist, export_cellxgene |
| Two or more datasets in one analysis | `multi_dataset` (merge_datasets first, then scVI on `dataset`) |
| Several samples/donors/lanes | `scvi_integration` (integrate_scvi) |
| Trusted labels for some cells | `scanvi_labels` (integrate_scanvi) |
| Condition effect with replicates | `condition_de` (pseudobulk_de); add memento_de for differential variability |
| 10x FASTQ, many samples or quick reprocessing | `fastq_kallisto` (kb_count) |
| 10x FASTQ, results must match common 10x practice | `fastq_cellranger` (cellranger_count; needs Cell Ranger in the library) |

- AnnData (`.h5ad`) with raw counts is the working format. Seurat objects are
  converted with `import_seurat` (Seurat is for compatibility, not the default).
- New FASTQ folders under `data/` are described with `register_fastq`; ask the
  student for the 10x chemistry (v2/v3), it is on the kit.
- Cluster markers from one sample are exploratory; condition DE needs
  replicates (pseudobulk_de or memento_de with replicate_key).
- A dataset without MT- genes (Kang 2018: removed upstream) cannot be filtered
  by % mito: the planner refuses `max_pct_mt` below 100. Tell the student, then
  set `max_pct_mt: 100`.
- If the dataset has trusted labels (authors' cell types), give
  annotate_celltypist `reference_key` so the result shows how CellTypist agrees.
- The warning `upstream_unused` means the DE result cannot change with the
  steps it names (e.g. DE per the dataset's `cell_type` after scVI/CellTypist):
  say so before the student compares such variants; group by
  `celltypist_majority_voting` to test the computed labels.
- scANVI: quote `holdout_accuracy` (labels hidden during training), never the
  agreement with training labels.
- Many variants: `sweep_branch(project, branch, step, param, values, sweep,
  reason)` makes one branch per value (shared steps computed once), then
  `submit_sweep`. Above the cap of active pipelines `submit_plan` and
  `submit_sweep` answer `status: "queued"`: sc-hub submits those plans itself
  when a pipeline ends (a branch goes as it is at that moment, re-planned if sc-hub
  or the branch changed meanwhile). Do not resubmit them; `queue_status` shows the
  queue and plans that could not be submitted, with the reason.
- Bookkeeping without new revisions: `label_branch` (tags, pinned, archived).
  The dashboard's Compare tab lists every branch and compares them.
- The SSH key only opens sc-hub (MCP server, dashboard, sessions). Do not try to
  ssh in for quotas, jobs or files: use `cluster_overview`, `run_status`,
  `run_logs`, `inspect_step`. If a request needs more, tell the student.
- After a change of a brick's code or a scientific package, finished branches show "needs re-run" on the
  dashboard: plan and submit them again. New code gives steps new keys, so most
  steps (GPU ones too) run again; say so before submitting.
- A subproject (`create_project("parent/child")`) may use another dataset; a
  project that starts from several datasets merges them in its first step.
- Extra software for the student's own notebook work: `add_project_packages`
  (pip packages on top of the shared environment, conda-forge/bioconda tools)
  builds a Jupyter kernel "sc-hub: <project>". Bricks keep the shared env.
- "How much space / how many jobs do I have?": `cluster_overview`.

## Interactive work

- `start_session(kind="jupyter", gpu=true)` for scvi-tools model work.
- "Show me the code" / "I want this as a notebook": `make_notebook(run_id)`
  writes the run as a notebook with every step's exact brick code and
  parameters; the student can change a step and re-run from there (results go
  to `notebooks/work/`). Open it with `start_session(kind="jupyter",
  target=<its path>)`, with `gpu=true` if it has scVI/scANVI steps. The
  dashboard also shows each step's code and offers the notebook as a download.
  A change the student wants to keep goes back into the branch with
  `revise_branch` or `fork_branch`.
- `export_cellxgene`, then `start_session(kind="cellxgene", target=<its
  cellxgene.h5ad>)` to explore cells and genes in the browser.
- The student opens a session with `./schub-lab jupyter` or `./schub-lab
  cellxgene` (Windows: `.\schub-lab.cmd ...`). A session holds one of their two
  running-job slots: use few hours and `stop_session` when they are done.

## Rules

- Quote numbers only from `run_results`. Never invent or estimate results.
- Do not SSH in to write sbatch scripts or to run analysis on the login node for
  anything a brick covers. If nothing covers the request, say so and propose a
  new brick instead of improvising a one-off pipeline.
- A missing dataset or index is downloaded with `fetch_asset` (a Slurm job),
  never on the login node and never into someone else's folder.
- Data stays on the cluster: do not copy matrices to this laptop or into the chat.
- Resubmitting the same plan is safe (it returns the existing run).
- Never read `sessions/*/connection.json` or run `schub session-info`: they hold the
  session token, which must not reach the chat. Only `schub-lab` on the laptop uses them.
