# sc-hub

Single-cell pipelines on the MBZUAI Slurm cluster, assembled from checked
building blocks ("bricks") by a coding agent (Codex, Claude Code) over MCP.
Everything runs under the student's own cluster account.

Pilot scope: scRNA-seq from a count matrix (`.h5ad`). Five bricks:

| Brick | What it does | Resources |
|---|---|---|
| `qc_filter` | QC metrics, cell/gene thresholds, Scrublet doublets | CPU |
| `normalize_embed` | normalize + log1p, seurat_v3 HVG, PCA, kNN, UMAP, Leiden | CPU |
| `integrate_scvi` | scVI batch integration, then kNN/UMAP/Leiden on the latent space | 1 GPU |
| `annotate_celltypist` | CellTypist labels with majority voting | CPU |
| `pseudobulk_de` | Sum counts per replicate x condition (x cell type), PyDESeq2 | CPU |

Starter assets fetched by bootstrap: 10x PBMC 3k, Kang 2018 (IFN-beta
stimulated PBMCs, 8 donors), CellTypist immune models.

## Install (student): one command

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
   their own; it opens only this cluster account, delete it to revoke) and the
   alias `mbzuai-schub`, and installs the key on the login node;
2. copies sc-hub to `/l/users/LOGIN/schub` and sets up the workspace there in a
   Slurm job (shared library environment, or a private one as a fallback);
3. connects sc-hub to every assistant it finds: **Codex** (also Codex inside the
   ChatGPT desktop app), **Claude Code** and **Claude Desktop**;
4. creates `~/sc-hub-workspace` with instructions for the assistant (`AGENTS.md`,
   `CLAUDE.md`) and `schub-view` for the dashboard (`schub-view.cmd` on Windows).

Running it again is safe: it replaces its own blocks in `~/.ssh/config` and the
Codex config (between `# >>> sc-hub >>>` markers), so a mistyped login is fixed
by rerunning with the right one. `SCHUB_KEY_PASSPHRASE=ask` makes the macOS/Linux
installer ask for a key passphrase instead (then keep the key in an ssh agent).

Then start your assistant there and just ask:

```bash
cd ~/sc-hub-workspace && codex      # or: claude
```

> "What datasets are in sc-hub? Create a project for my question: how does the
> IFN-beta response differ between PBMC cell types?"

ChatGPT in the browser cannot connect: it only talks to public HTTPS MCP
servers, which this pilot deliberately does not run.

The MCP server is a short-lived process on the login node that talks JSON-RPC
over the SSH channel. It only reads metadata and calls Slurm; analysis runs in
jobs. Your remote `~/.bashrc` must not print anything for non-interactive shells
(the installer checks this). The same operations exist as a CLI on the cluster:

```bash
~/schub/bin/schub inspect kang2018
~/schub/bin/schub branch-plan ifn-response main
~/schub/bin/schub submit <plan_id>
```

## For the pilot owner

| Task | Command |
|---|---|
| Publish or update the shared library (env, datasets, models) | `bash scripts/publish_library.sh` on the cluster |
| ...inside an allocation you already hold | `SCHUB_SRUN_ARGS="--jobid=<id> --overlap" SCHUB_GPU_CHECK=0 bash scripts/publish_library.sh` |
| Build and publish the installers | `bash scripts/release.sh` on your laptop (`--gist` also updates a secret gist) |
| Run the tests | `.venv/bin/python -m pytest --cov=schub` |

Code changes reach students when the library is published; everyone re-runs the
installer (idempotent) or just `bootstrap_cluster.sh` to pin the new environment.

## Why MCP instead of letting the agent SSH around

A capable agent with a shell can install scanpy and write sbatch scripts. What
it does not give a lab is the following, which is what sc-hub adds:

- **Checks that are enforced, not suggested.** Planning refuses CellTypist on
  unnormalized data, scVI integrating over the condition under study, DE
  without biological replicates or with clusters used as replicates, a
  mitochondrial filter that silently matches nothing, log data fed to
  count-based methods. Jobs refuse a GPU step when torch cannot see the GPU
  instead of quietly training on CPU. An AGENTS.md can ask for this; a plan
  validator guarantees it.
- **Scheduler hygiene.** Dependency chains with `--kill-on-invalid-dep`, no
  duplicate submissions on retry, cached step reuse, a per-user cap on active
  pipelines, nothing heavy on the login node.
- **One environment and one layout for the lab.** Results from different
  students are comparable because they come from the same brick versions.
- **Provenance and measurement.** Every run keeps plan, params, versions and
  logs; every tool call is logged, which is how the pilot shows its value.
- **Other clients.** ChatGPT cannot SSH; a remote MCP endpoint (phase 2, with
  the HPC team) reuses the same tools.

The logic lives in the `schub` package, not in the MCP layer, so the CLI and
the MCP tools enforce the same rules. Off-road work (anything no brick covers)
is still possible over plain SSH; the fix for recurring off-road work is a new brick.

## Shared library and the fallback

The pilot owner publishes a read-only library (`scripts/publish_library.sh`):

```
/l/users/<owner>/sc-hub-library/
  envs/<version>/  envs/current     Python env (students pin the resolved version)
  hub/<version>/                    sc-hub source snapshot
  datasets/<name>/                  data.h5ad + dataset.yaml (license, sha256)
  models/celltypist/                CellTypist models
```

`bootstrap_cluster.sh` uses it when the student can read it: no private
environment (saves ~6 GB each) and no duplicate datasets. If the library is
missing or unreadable, bootstrap builds a private environment and downloads
everything into `$SCHUB_ROOT/library-local`. If the library lacks one asset, only
that asset is downloaded there. At any time `fetch_asset` (MCP) queues a
download job. Datasets with a recorded checksum get the same cache keys whether
they come from the library or a fallback copy.

## Projects, branches, ideas

```
projects/<project>[/<subproject>]/
  project.yaml            question, status, datasets
  pipelines/<branch>.yaml steps, or `from: main` + overrides (+ append)
  ideas/<slug>.md         status, hypothesis, reverses_if, linked branches
  logbook.md              dated entries; each completed run adds one
  runs/<run_id>           links to runs submitted from this project
```

A branch is a pipeline variant. Step outputs live in `cache/steps/<key>`, where
the key hashes the input, brick code and version, params and environment, so
branches share their common prefix: `latent-10` (from `main` with
`integrate_scvi.n_latent: 10`) reuses main's QC and normalization.

## Dashboard on a weak laptop

`schub dashboard` (about 3 s on the login node) writes `view/`: one static
`index.html` (about 100 KB) plus small images. Six views: overview, pipelines,
runs, jobs, library, projects. Pick a branch to see its graph, click a step to
see its params, job, resources, timing, log tail, figures and results; open a run
for its step timeline and top DE genes. Plain HTML, CSS and 3 KB of vanilla JS:
no server, no framework, no external requests. `./schub-view` mirrors it every
60 s over ssh + rsync (`schub-view.cmd` with scp on Windows, skipping the
download when nothing changed); the page refreshes itself, keeps your place and
filters, and waits while you read an opened section.

## Cluster limits that shape the design

- QOS `ia-std` on `ws-ia`: at most 2 running jobs, 24 CPUs and ~107 GB per user.
  An interactive `personal-ws` job takes one of the two slots. Plans are clamped
  to 24 CPUs / 100 GB per step; extra steps simply wait (`QOSMaxJobsPerUserLimit`).
- `sacct` does not work from the login node; states come from squeue, scontrol
  and step markers.

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
```

Unit tests use a fake Slurm and tiny generated `.h5ad` files. The brick
implementations are exercised end to end on the cluster.

## Known limits (pilot)

- torch is pinned to CUDA 12.8 wheels (`SCHUB_TORCH_BACKEND`): the node driver
  (570) cannot run the default cu130 builds. Bootstrap verifies this in a GPU job.
- Downloads run inside the bootstrap job, not on the login node (its Lustre
  client logged write errors during testing).

- Species and gene-id detection are heuristics; plans accept overrides.
- Linear pipelines only; FASTQ -> counts (nf-core/scrnaseq) is out of scope.
- The Windows installer and `schub-view.cmd` / `.ps1` have not been run on Windows yet.
- The notebook tunnel needs the login node to reach compute-node ports.
