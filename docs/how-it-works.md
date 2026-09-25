# How sc-hub works

The bench, the journal, reports, the lab agent and the dashboard, and why they are built this way. Back to the [README](../README.md).

## How it works

The agent's one way to act is a **cell**: code with a required *why* and
*expect*, run in a live Jupyter kernel inside a Slurm job (the *workbench*, CPU
only, on a compute node). Each cell becomes an entry in the project's journal
with its outputs, figures, the files it wrote, its downloads, the Slurm jobs it
sent and the checks it passed or failed. The journal is the source of truth: the
dashboard's Journal tab shows it, a new chat starts from its hand-over, and a
notebook of the project can be downloaded from it. Once the study is done, a
**report** tells it to a reader: a notebook assembled from the journal (below).

```
chat (Claude Code / Codex / Claude Desktop)
  └─ MCP over SSH ─ login node: sc-hub MCP server (short-lived, small files only)
                      └─ bench/inbox on Lustre ─ workbench job: runner + one kernel per project
                                                   └─ %%slurm cells ─ their own jobs (GPU, hours)
```

MCP tools: `projects`, `create_project`, `run`, `wait`, `journal`, `note`,
`handoff`, `report`, `datasets`, `files`, `skills`, `cluster`, `stop`. Each answer comes
back within the client's time limit (35 s for Claude clients, 45 s for Codex);
a longer cell answers "running" and `wait(ref)` picks it up, never re-running it.

Inside a cell, `bench` offers:

| Helper | What it does |
|---|---|
| `bench.fetch(url, md5=..., sha256=...)` | resumable download, checksum verified, url/size/sha256 recorded in the journal |
| `bench.twin(path, stratify=, keep=[controls])` | a small stratified copy of a dataset (controls kept) to try code on in seconds |
| `%%slurm --gpus 1 --time 6h ...` | the cell as its own Slurm job, frozen with a checksum; its state, files and checks come back to the same entry |
| `bench.run_brick(name, ...)` | one of sc-hub's checked single-cell steps ([bricks](bricks.md)) |
| `bench.clone(url, ref)`, `bench.repo_env(...)` | a paper's code at a recorded commit, in its own uv environment (torch from the cu118–cu128 builds) |
| `bench.packages(pip=[...], conda=[...])` | what the shared environment lacks, built in the cell for this project; its next cell starts a fresh kernel with it |
| `bench.import_seurat(rds, name)` | a Seurat object as `data/<name>/data.h5ad`; R + Seurat come from the library or are built once into the student's own |
| `from schub_ckpt import Run` | checkpoints that survive time limits: fsynced files, complete.json, latest.json; resume refuses another registration |
| `bench.compare(ours, paper, source=...)` | our numbers next to a paper's, with a tolerance per metric |

`run(..., checks=[...])` validates what a cell produced: files, table columns,
finite values, raw counts, obs columns, cells per group, a DE design with real
replicates, a Perturb-seq screen (controls, cells per perturbation, knockdown of
each target after library-size normalization), a visible GPU. A failed check
marks the entry. Numbers in findings and verdicts are traced to the outputs of
the cells they cite; untraced ones are flagged.

**Skills** (`skills()`): resume, rigor, mbzuai_slurm, slurm_jobs, checks,
fetching_data, dataset_sources, perturbseq, twins, paper_reproduction,
report_writing, bricks_library.

**Reports.** The journal is the protocol (every step, dead ends included); a
report is the story for a student or a professor, like the VCC2026 education
notebooks. The author writes a spec: title, summary and blocks (`{"text"}`,
`{"cell": "c0012", "show": "outputs"}`, `{"figure": "c0015"}`, `{"note": "n0007"}`).
sc-hub assembles the notebook with code, recorded outputs, figures and notes taken
verbatim from the journal, adds where the data came from and an appendix of the
findings, verdicts, decisions and mistakes the text leaves out, and writes an HTML
copy without code. Checks are lenient: only a block pointing at nothing refuses the
report; untraced numbers, failed checks, twin data and files overwritten later are
warnings the reader sees at the end. `report(project, spec)` builds
`reports/draft/`, `publish=true` keeps `reports/NN-<title>/` unchanged for good.

**The lab agent.** A standing goal (`goal/goal.md`) can be worked on without a
chat open. The student's own assistant hands it over with `delegate(project, objective,
deliverables, max_turns)` after agreeing the task with the student, and follows it with
`delegation(project)` (the owner can also run `schub goal-start <project> --goal goal.md`).
In `mode: free` (what `delegate` writes) the agent also has its engine's own tools: the
shell, file edits and subagents. It reads the whole project but writes only in the
project's `work/` and a temp folder, never its `data/`, `journal/` or `goal/`: its shell
runs in the engine's OS sandbox, which also hides keys, sign-in files, tokens and other
chats' transcripts, and file edits are accepted only there. After each turn its commands,
their output and the files it changed become one journal cell. `mode: bench` (a goal.md
without `mode`, and the evaluation matrix) keeps it to the sc-hub tools. It runs the CLI's newest model at effort `xhigh`
unless the owner's engine policy pins others. It queues Slurm slices; each
arms its successor first, skips the model while it waits on its own jobs, and
runs one turn of Claude Code or Codex through the same MCP tools (so its cells
are journal entries too, marked with the engine). The owner's engine policy
(`schub engine-policy set mixed|claude-only|codex-only`, per-project grants,
pinned models) decides which engine may run; a usage limit pauses that engine
until its reset and, in `mixed`, the other takes the turn, starting from the
journal's hand-over. Claude stands down while its seven-day window is 80% full
(`claude_weekly_ceiling`; the student's own chats share it). A turn that fails before
doing any work (a lost sign-in) is not counted; failures in a row pause the engine
1 h, 6 h, then 24 h, each pause with a note (and a dashboard alert) telling the student
how to sign in again (`onboard/start.sh retry agents`). A new sign-in is noticed within
the goal's pace, and `schub goal-start` tries the engine at once. A finished or stopped
goal takes its queued cells back, and a job Slurm will never start (or one waited on for
a day) wakes the model instead of an endless wait. The watchdog re-arms a broken chain
and probes the engines.
When a turn hands the work over as complete, a writer turn (a fresh session that
reads only the journal and cannot reopen the work) publishes the report; after two
turns without one the goal ends anyway and a note says so. `report: no` in
goal.md turns this off; `schub goal-report <project>` writes one for a project
finished earlier or in chat.

**Breadth evaluation.** `evals/requests.yaml` holds 18 requests (the two
acceptance requests, VCC-style perturbation work, course-style tasks);
`schub eval-run` starts each as a lab-agent goal per engine and `schub
eval-score` scores the request x engine matrix from the journals.

The workbench stops itself when idle (its slot on ws-ia is one of two); the next
cell starts it again. Background jobs (the watchdog, lab-agent slices) run
CPU-only on the gpu partition's own budget, with ws-ia as the fallback.

## Why MCP instead of letting the agent SSH around

A capable agent with a shell can install scanpy and write sbatch scripts. What
it does not give a lab is the following, which is what sc-hub adds:

- **A journal instead of an invisible SSH session.** Every cell says why it runs
  and what it should show; its outputs, files, downloads, jobs and checks are
  recorded, and the student reads them on the dashboard while the agent works.
- **Checks that are enforced, not suggested.** A cell's checks (a Perturb-seq
  screen's knockdown, a DE design with real replicates, raw counts) mark its
  entry when they fail. In brick pipelines, planning refuses CellTypist on
  unnormalized data, scVI integrating over the condition under study, DE
  without biological replicates or with clusters used as replicates, a
  mitochondrial filter that silently matches nothing, log data fed to
  count-based methods. It warns when a merge drops one dataset's MT- genes
  (each cell's % mito is kept from before the join), and when a DE step cannot
  depend on the analysis steps before it (grouping by the dataset's own cell
  types after CellTypist: variants of scVI or CellTypist give identical DE).
  Jobs refuse a GPU step when torch cannot see the GPU instead of quietly
  training on CPU. An AGENTS.md can ask for this; a plan validator guarantees it.
- **Numbers that mean what they say.** DE counts genes, not gene x cell-type
  pairs; scANVI reports accuracy on held-out labels, not on its training labels;
  CellTypist can be checked against the authors' labels (`reference_key`).
- **Scheduler hygiene.** Dependency chains with `--kill-on-invalid-dep`, no
  duplicate submissions on retry, cached step reuse, a per-user cap on active
  pipelines with sc-hub's own queue above it, nothing heavy on the login node.
- **A key that only opens sc-hub.** The agent's passphrase-less key runs the
  MCP server and the dashboard mirror, nothing else (`schub-gate`).
- **One environment and one layout for the lab.** Results from different
  students are comparable because they come from the same brick versions.
- **Provenance and measurement.** Every run keeps plan, params, versions and
  logs; every tool call is logged, which is how the pilot shows its value.
- **Other clients.** ChatGPT cannot SSH; a remote MCP endpoint (phase 2, with
  the HPC team) reuses the same tools.

The logic lives in the `schub` package, not in the MCP layer, so the CLI and
the MCP tools enforce the same rules. Anything no brick covers is ordinary code
in a cell, with the same journal and checks.

## Projects and the journal

```
projects/<project>/
  project.yaml            question, status, datasets
  work/                   where cells run and write
  data/                   downloads (bench.fetch), twins in data/twins/<hash>/
  journal/cells/          one JSON per cell (single writer) + addenda (job, files, checks)
  journal/notes/          registrations, decisions, findings, verdicts, errors, incidents
  journal/handoff.md      where the work stands (at most 120 lines); checkpoint.json
  jobs/<cell>-<key>/      %%slurm snapshots, logs and results
  goal/                   a lab agent's goal.md and state
  reports/draft/          the latest report draft (replaced by each build)
  reports/NN-<title>/     a published report: report.ipynb, report.html, spec.json, report.json
```

Brick-era projects also have:

```
projects/<project>[/<subproject>]/
  pipelines/<branch>.yaml steps, or `from: main` + overrides (+ append)
  ideas/<slug>.md         status, hypothesis, reverses_if, linked branches
  logbook.md              dated entries; each completed run adds one
  runs/<run_id>           links to runs submitted from this project
```

A branch is a pipeline variant. Step outputs live in `cache/steps/<key>`, where
the key hashes the input, the brick's code and version, params and the versions
of the scientific packages (not the sc-hub version), so
branches share their common prefix: `latent-10` (from `main` with
`integrate_scvi.n_latent: 10`) reuses main's QC and normalization.

## Dashboard on a weak laptop

`schub dashboard` (about 5 s on the login node) writes `view/`: a small
`index.html` plus one file per project page (`jproj/`), loaded when the project is
picked, so the page stays light with a hundred projects. It opens on the Journal: on
the left, a navigator with a search box and projects grouped by where they stand
(needs you, in progress, done, quiet for a week, evaluation runs), variants
(`project/variant`) as a tree under their project. A project's page starts with the
question, where it stands and the outcome (the report's summary or the hand-over),
with links to the report, its notebook and the protocol notebook; then its
variants, its findings and decisions one line each, and its steps one line each
(status, why, jobs, checks, figure), which open on a click with the output, figures,
code and files. Filters show failed steps, steps with figures or one engine's work;
system events stay hidden unless asked for. Alerts say when the workbench cannot start, both
job slots are taken, a check failed or a quota is nearly full. Runs of brick
pipelines, the library and the cluster overview are in the menu; open a run for
its step timeline, each step's params, code, job, log tail, figures, top DE genes
and its notebook. Plain HTML, CSS and a few KB of vanilla JS:
no server, no framework, no external requests. `./schub-view` mirrors it every
60 s over ssh + rsync (`schub-view.cmd` with scp on Windows, skipping the
download when nothing changed); the page refreshes itself, keeps your place and
filters, and waits while you read an opened section.

## Cluster limits that shape the design

- QOS `ia-std` on `ws-ia`: at most 2 running jobs, 24 CPUs and ~107 GB per user.
  An interactive `personal-ws` job and the workbench can take both slots; a job
  sent to full slots says so at once (`QOSMaxJobsPerUserLimit`).
- QOS `gpu-1` on `gpu`: 16 CPUs, 90 GB and 1 GPU per user, 8 h per job, no cap
  on the number of jobs. The watchdog and lab-agent slices run there without a
  GPU; their start is not guaranteed (ws-ia is the fallback).
- Node driver 570: CUDA ≤ 12.8; `bench.repo_env` refuses cu13x torch builds.
- `sacct` does not work from the login node; states come from squeue, scontrol
  and step markers.

## Known limits (pilot)

- torch is pinned to CUDA 12.8 wheels (`SCHUB_TORCH_BACKEND`): the node driver
  (570) cannot run the default cu130 builds. Bootstrap verifies this in a GPU job.
- Downloads run inside the bootstrap job, not on the login node (its Lustre
  client logged write errors during testing).

- Species and gene-id detection are heuristics; plans accept overrides.
- Linear pipelines only. FASTQ support covers 10x droplet chemistries (v1-v4);
  STARsolo and Salmon/Alevin are not wrapped.
- The lab agent's engines use the student's own Claude Code and Codex logins on
  the cluster; the journal records only a fingerprint of the login files. Both
  run with their own tools off (Claude `--tools ""`; Codex shell, browser,
  computer use, apps and sub-agents disabled) and only the sc-hub MCP server.
- The engine guards sit first on the PATH of lab-agent slices and of kernels,
  but everything runs as the same Unix user: the policy and the turn budget stop
  accidents and casual bypasses, not a determined one (that needs a separate user
  or a container).
- Sessions listen on the compute node behind a random token (compute nodes take
  no ssh logins, so the tunnel ends at node:port through the login node).
- Cell Ranger is only available after the owner installs it (10x license).
- The Windows installer, `schub-view.cmd` and `schub-lab.cmd` have not been run on Windows yet; the setup
  page's Windows paths (the password window, the launcher) are tested against a fake ssh only.
- The setup page's sign-ins on the cluster were checked up to the browser (installers, links, codes, the
  tunnel, a wrong code) in a throwaway home; a full sign-in needs a student account.
