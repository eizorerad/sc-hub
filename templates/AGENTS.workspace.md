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
3. `save_branch` for the pipeline (name the first one `main`). For a variant,
   save a new branch with `from_branch` + `overrides`; shared steps are reused
   from cache. Show the student the plan, its warnings and GPU-hours. If it has
   errors, explain them; do not bypass them.
4. `submit_plan`, then check `run_status` every few minutes, not in a tight loop.
5. `run_results` for numbers (this also writes the project logbook), then
   `make_dashboard`; the student sees it with `./schub-view`.
6. Hypotheses go in `add_idea` (with `reverses_if`), decisions in
   `add_logbook_entry`. Link ideas to branches with `update_idea`.

## Rules

- Quote numbers only from `run_results`. Never invent or estimate results.
- Do not SSH in to write sbatch scripts or to run analysis on the login node for
  anything a brick covers. If nothing covers the request, say so and propose a
  new brick instead of improvising a one-off pipeline.
- A missing dataset is downloaded with `fetch_asset` (a Slurm job), never on the
  login node and never into someone else's folder.
- Data stays on the cluster: do not copy matrices to this laptop or into the chat.
- Resubmitting the same plan is safe (it returns the existing run).
