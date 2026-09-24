# sc-hub

A lab bench for single-cell and computational biology on the MBZUAI Slurm
cluster, driven by a coding agent (Claude Code, Codex, Claude Desktop) over MCP.
Everything runs under the student's own cluster account, and everything the
agent does lands in a journal the student can read.

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
| `bench.run_brick(name, ...)` | one of sc-hub's checked single-cell steps (below) |
| `bench.clone(url, ref)`, `bench.repo_env(...)` | a paper's code at a recorded commit, in its own uv environment (torch from the cu118–cu128 builds) |
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
chat open: `schub goal-start <project> --goal goal.md` queues Slurm slices; each
arms its successor first, skips the model while it waits on its own jobs, and
runs one turn of Claude Code or Codex through the same MCP tools (so its cells
are journal entries too, marked with the engine). The owner's engine policy
(`schub engine-policy set mixed|claude-only|codex-only`, per-project grants,
pinned models) decides which engine may run; a usage limit pauses that engine
until its reset and, in `mixed`, the other takes the turn, starting from the
journal's hand-over. The watchdog re-arms a broken chain and probes the engines.
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

## Set up (student): the setup page

The recommended way. From a copy of sc-hub on your laptop, run one command (or
paste the sc-hub link into Codex or Claude Code and ask it to set you up: the
repository's `AGENTS.md` tells it to start this page and hand it to you):

```bash
sh onboard/start.sh                                             # macOS, Linux
powershell -ExecutionPolicy Bypass -File onboard\start.ps1      # Windows 10/11
```

A page opens on this computer only (`127.0.0.1`, with a one-time token in the
link). It shows a progress bar and one line per step, and asks you for
something only where it has to:

1. **Your browser for the sign-ins.** The agents on the cluster sign in with
   your MBZUAI student ChatGPT and Claude accounts: make the browser where those
   are signed in your default one (or copy the links into it).
2. **Sign in to the cluster, once.** Your login and password. The password goes
   straight to `ssh` on this computer to install a dedicated key
   (`~/.ssh/mbzuai_schub_ed25519`, alias `mbzuai-schub`); it is never stored and
   never reaches the assistant. On Windows 10, whose OpenSSH does not take a
   password from a program, a console window opens and you type it there.
3. **sc-hub on the cluster**, in `/l/users/LOGIN/schub`, set up in a Slurm job.
4. **A first run**: a kernel cell and a small Slurm job with a check, in the
   project `hello`.
5. **Your assistants**: Codex (also in the ChatGPT desktop app), Claude Code and
   Claude Desktop get the sc-hub MCP server; `~/sc-hub-workspace` is created.
6. **VS Code in your workbench job**: the host `mbzuai-schub-ide` gives VS Code
   (Remote-SSH) a shell and files inside your own workbench job, which it starts
   if needed; set up once, it works for every later job.
7. **Codex and Claude Code on the cluster**, for the lab agent. Both are
   installed with their official installers and signed in from your browser:
   Codex shows a one-time code (if your workspace turned device codes off, it
   signs in the usual way through a short-lived tunnel to `localhost:1455`),
   Claude shows a code to paste back into the page. The page then shows which
   account each one uses, so you can check it is the student one.
8. **The key only opens sc-hub** (see below); your password login is unchanged.
9. **The dashboard mirror** starts and opens in your browser.

A step that fails says what to do and has a Retry button; running the page again
skips what is done. `--home DIR` writes everything under `DIR` instead of your
home (for trials), `--no-browser` opens nothing by itself.

## Install (student): one command

The older installer, without the page (no VS Code or cluster agents setup).

Run it on your laptop in a terminal. Replace `LOGIN` with your cluster login;
your cluster password is asked once or twice, never stored.

macOS / Linux:

```bash
ssh LOGIN@login-student-lab.mbzu.ae cat /l/users/leonid.klarov/sc-hub-library/install/install-sc-hub.sh | bash -s -- LOGIN
```

Windows 10/11 (PowerShell, built-in OpenSSH client):

```powershell
ssh LOGIN@login-student-lab.mbzu.ae cat /l/users/leonid.klarov/sc-hub-library/install/install-sc-hub.ps1 | Out-String | iex
```

The installer lives in the pilot owner's shared library on the cluster, so only
people with a cluster account can download it. It:

1. creates a dedicated SSH key (no passphrase, so the assistants can connect on
   their own) and the alias `mbzuai-schub`, and installs the key on the login
   node; at the end it limits the key to sc-hub (see below);
2. copies sc-hub to `/l/users/LOGIN/schub` and sets up the workspace there in a
   Slurm job (shared library environment, or a private one as a fallback);
3. connects sc-hub to every assistant it finds: **Codex** (also Codex inside the
   ChatGPT desktop app), **Claude Code** and **Claude Desktop**;
4. creates `~/sc-hub-workspace` with instructions for the assistant (`AGENTS.md`,
   `CLAUDE.md`) and `schub-view` for the dashboard (`schub-view.cmd` on Windows).

The key opens sc-hub only: its line in `~/.ssh/authorized_keys` on the cluster
starts with `restrict,port-forwarding,command="<root>/bin/schub-gate"`, which
lets through the MCP server, the dashboard mirror (`schub dashboard` + a
read-only rsync of `view/`) and `schub-lab` sessions (their tunnel), and
refuses the rest, a shell included; every decision goes to
`<root>/logs/gate.log`. This keeps an assistant on sc-hub's checked, logged
tools (quotas and jobs come from `cluster_overview`, not from a shell). It is
not a sandbox: sc-hub itself runs the student's code (pipelines, Jupyter
sessions), as it should. The student's own login (password) is unaffected.
Delete that line to revoke the key; re-running the installer with
`SCHUB_KEY_UNRESTRICTED=1` makes it a normal key again (not recommended). The
Windows installer does not limit the key yet.

Running it again is safe: it replaces its own blocks in `~/.ssh/config` and the
Codex config (between `# >>> sc-hub >>>` markers), so a mistyped login is fixed
by rerunning with the right one. `SCHUB_KEY_PASSPHRASE=ask` makes the macOS/Linux
installer ask for a key passphrase instead (then keep the key in an ssh agent).

Then start your assistant there and just ask:

```bash
cd ~/sc-hub-workspace && codex      # or: claude
```

> "Create a project for my question: how does the IFN-beta response differ
> between PBMC cell types? Start from Kang 2018 in the library."

ChatGPT in the browser cannot connect: it only talks to public HTTPS MCP
servers, which this pilot deliberately does not run.

The MCP server is a short-lived process on the login node that talks JSON-RPC
over the SSH channel. It only reads and writes small files and calls Slurm;
cells run in the workbench job. Your remote `~/.bashrc` must not print anything
for non-interactive shells (the installer checks this). The same operations
exist as a CLI on the cluster:

```bash
~/schub/bin/schub bench-run ifn --code 'print(1)' --why "a first cell" --expect "1"
~/schub/bin/schub bench-journal ifn
~/schub/bin/schub bench-status
```

## For the pilot owner

| Task | Command |
|---|---|
| Publish or update the shared library (env, datasets, models) | `bash scripts/publish_library.sh` on the cluster |
| ...inside an allocation you already hold | `SCHUB_SRUN_ARGS="--jobid=<id> --overlap" SCHUB_GPU_CHECK=0 bash scripts/publish_library.sh` |
| Build and publish the installers | `bash scripts/release.sh` on your laptop (`--gist` also updates a secret gist) |
| Install Cell Ranger (10x license; link from the 10x downloads page) | `bash scripts/install_cellranger.sh '<link>' human` on the cluster |
| Choose the lab agents' engines (owner only) | `schub engine-policy set mixed --primary claude`, `... grant <project> codex`, `schub engine-probe` |
| Give a project a lab agent | `schub goal-start <project> --goal goal.md`; `schub goal-status` / `goal-stop` |
| Write the report of a finished project | `schub goal-report <project>` (a writer turn in a Slurm slice) |
| Run the breadth evaluation | `schub eval-run evals/requests.yaml --engines claude,codex`, later `schub eval-score evals/requests.yaml --markdown` |
| Run the tests | `.venv/bin/python -m pytest --cov=schub`, then `tests/e2e/run.sh` for the dashboard |

Code changes reach students when the library is published; everyone re-runs the
installer (idempotent) or just `bootstrap_cluster.sh` to pin the new environment.

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

## Shared library and the fallback

The pilot owner publishes a read-only library (`scripts/publish_library.sh`):

```
/l/users/<owner>/sc-hub-library/
  envs/<version>/  envs/current     Python env (students pin the resolved version)
  hub/<version>/                    sc-hub source snapshot
  datasets/<name>/                  data.h5ad + dataset.yaml, or FASTQ + fastq.yaml (license, sha256)
  models/celltypist/                CellTypist models
  refs/kallisto/<organism>/         prebuilt kallisto|bustools index + t2g
  refs/cellranger/<organism>/       10x references (only with Cell Ranger)
  tools/cellxgene/<version>/        cellxgene in its own venv (it pins numpy 2.0.1)
  tools/r-seurat/<version>/         R + Seurat (conda-forge, pinned micromamba)
  tools/cellranger/<version>/       optional, installed by the owner
```

`bootstrap_cluster.sh` uses it when the student can read it: no private
environment (saves ~6 GB each) and no duplicate datasets. If the library is
missing or unreadable, bootstrap builds a private environment and downloads
everything into `$SCHUB_ROOT/library-local`. If the library lacks one asset, only
that asset is downloaded there. At any time `fetch_asset` (MCP) queues a
download job. Datasets with a recorded checksum get the same cache keys whether
they come from the library or a fallback copy.

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

## Brick pipelines (legacy, `SCHUB_LEGACY_TOOLS=1`)

Before the bench, sc-hub assembled whole pipelines from checked bricks. Their MCP
tools (`plan_pipeline`, `save_branch`, `plan_branch`, `submit_plan`, `run_status`,
`run_results`, `make_notebook`, sessions, FASTQ registration, Seurat import) appear
only with `SCHUB_LEGACY_TOOLS=1`; the bricks themselves stay available in cells
through `bench.run_brick`. Recipes, sweeps, branch revisions and forks as tools,
labels, sc-hub's own plan queue and the Projects, Pipelines and Compare tabs were
removed once the bench took over (tag `v0.5-bricks` has them). At the cap of active
pipelines `submit_plan` now refuses with the reason instead of queueing. Runs of
brick pipelines, with their steps, figures and notebooks, stay under Runs in the
menu.

| Brick | What it does | Resources |
|---|---|---|
| `kb_count` | FASTQ -> counts with kallisto\|bustools (prebuilt human/mouse index), knee cell calling | CPU |
| `cellranger_count` | FASTQ -> counts with 10x Cell Ranger (if the owner installed it) | CPU, 64 GB |
| `qc_filter` | QC metrics, cell/gene thresholds, Scrublet doublets | CPU |
| `normalize_embed` | normalize + log1p, seurat_v3 HVG, PCA, kNN, UMAP, Leiden | CPU |
| `integrate_scvi` | scVI batch integration, then kNN/UMAP/Leiden on the latent space | 1 GPU |
| `integrate_scanvi` | scVI + scANVI with known labels; predicted labels for every cell, accuracy on held-out labels | 1 GPU |
| `annotate_celltypist` | CellTypist labels, majority voting over CellTypist's own over-clustering; optional comparison with known labels | CPU |
| `pseudobulk_de` | Sum counts per replicate x condition (x cell type), PyDESeq2 | CPU |
| `memento_de` | memento: differential mean and variability (method of moments) | CPU |
| `export_cellxgene` | Subsampled, normalized `.h5ad` for cellxgene | CPU |

Starter assets fetched by bootstrap: 10x PBMC 3k, Kang 2018 (IFN-beta
stimulated PBMCs, 8 donors), CellTypist immune models. The library also holds
prebuilt kallisto indices (human, mouse) and 10x PBMC 1k v3 FASTQ reads.

## Adding a brick

1. `src/schub/bricks/<name>.py`: pydantic params, `check` (planning-time
   issues), `transform` (effect on the dataset state), `resources`, `SPEC`.
2. `src/schub/bricks/impl/<name>.py`: `run(io, params) -> summary dict`, heavy
   imports inside; raise `BrickError` for data-level failures.
3. Register it in `src/schub/bricks/__init__.py`; add planner tests.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest --cov=schub
tests/e2e/run.sh                         # the dashboard in Chrome: every control
tests/e2e/run.sh ~/sc-hub-workspace/view # ...or the mirror of the real one
```

Unit tests use a fake Slurm and tiny generated `.h5ad` files. The brick
implementations are exercised end to end on the cluster. The dashboard test
(Node 18+ and Google Chrome; it installs `playwright-core` on first run) clicks
every tab, menu, step, filter and copy button of a page opened as a file, as
students open it, and checks what each does: addresses, requests copied,
notebooks downloaded, the auto-refresh keeping the open step, a 375 px phone.
Results and screenshots go to `tests/e2e/out/`.

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
