---
name: perturbseq
description: Perturb-seq / CRISPRi screens (Replogle K562, Norman): getting the data, the perturbation table, QC with knockdown checks.
---
# Perturb-seq screens

## Data (checked 2026-09, scPerturb, Zenodo record 10044268, v1.3, CC-BY-4.0)

| File | Size | md5 |
|---|---|---|
| `ReplogleWeissman2022_K562_essential.h5ad` | 1.55 GB | `d8cba17576d1a8afc0f7d71b79cad0f7` |
| `ReplogleWeissman2022_rpe1.h5ad` | 1.24 GB | `cc7f1ec50aeb3a3e1b4a6cfa713d80fa` |
| `NormanWeissman2019_filtered.h5ad` | 0.70 GB | `c870e6967d91c017d9da827bab183cd6` |
| `ReplogleWeissman2022_K562_gwps.h5ad` | 8.81 GB | `13db594f8f1d2ccb88fec44a13e414dc` |

URL: `https://zenodo.org/records/10044268/files/<file>?download=1`. Fetch with the
md5 (`bench.fetch(url, dest=bench.data_dir() / "<file>", md5="...")`), inside a
`%%slurm` cell for the large ones. Start with K562 essential (~2,000 essential
genes): the genome-wide screen has millions of cells and does not fit in a
workbench's memory (read it backed, or work per perturbation).

## The perturbation table

- Look before assuming: in scPerturb files the obs column `perturbation` names the
  targeted gene and non-targeting cells are usually `control`; check the actual
  columns and levels with `datasets(name)` or a cell (`adata.obs.columns`,
  `value_counts()`), and confirm the control label with the student.
- One row per perturbation: target gene, cells, guides (if a guide column exists),
  median UMI / genes per cell, and knockdown of the target (its expression in these
  cells vs controls, after library-size normalization). Save it as CSV.

## QC

1. `bench.twin(path, stratify="perturbation", keep=["control"])` first: a few % of
   each perturbation plus the controls. Write and debug the QC there.
2. The QC itself: cells per perturbation, controls present, cell-level QC (genes,
   UMIs, % mito), and knockdown per target.
3. Ask for the check so the numbers are verified, not asserted:
   `checks=[{"name": "perturbation", "params": {"path": "...", "perturbation":
   "perturbation", "control": "control"}}]` (skills("checks")).
4. The same code on the full data as a `%%slurm` job, with the same checks.
5. Record the findings with the cells they come from; a target without knockdown
   is a finding about the screen, not something to drop silently.
