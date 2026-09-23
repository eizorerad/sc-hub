---
name: bricks_library
description: sc-hub's checked single-cell steps (QC, normalization, scVI, CellTypist, pseudobulk DE...) as functions in a cell.
---
# sc-hub's bricks as functions

The course's standard steps are available as checked functions:

```
result = bench.run_brick("qc_filter", "../data/pbmc.h5ad", "pbmc.qc.h5ad", {"min_genes": 200})
result["summary"]        # what the step measured (cells kept, thresholds...)
```

Bricks: qc_filter, normalize_embed, integrate_scvi (GPU), integrate_scanvi (GPU),
annotate_celltypist, pseudobulk_de, memento_de, merge_datasets, export_cellxgene.
`datasets(name)` shows a dataset's columns; `run_brick` refuses data a brick cannot
handle (e.g. DE without replicates, CellTypist on unnormalized data) with the reason.

- They are the fast lane for standard steps: prefer them over hand-written code for
  the same thing, so results stay comparable across students.
- GPU bricks refuse to run on the CPU kernel: put the call in a `%%slurm --gpus 1`
  cell (`skills("slurm_jobs")`).
- Anything the bricks do not cover: write the code yourself in cells. That is
  normal; the journal records it the same way.
- FASTQ counting (kb_count, cellranger_count) is not a cell function; it needs a
  job with the reads' folder.
