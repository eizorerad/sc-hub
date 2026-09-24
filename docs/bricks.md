# Brick pipelines (legacy)

sc-hub's checked single-cell steps, still available in cells through `bench.run_brick`. Back to the [README](../README.md).

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
