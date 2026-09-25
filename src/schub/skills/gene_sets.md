---
name: gene_sets
description: Pathway and gene-set scores (MSigDB Hallmark with decoupler): per cell, per pseudobulk sample, or from a DE table.
---
# Gene sets and pathway scores

## The sets (checked 2026-09, MSigDB 2025.1)

| Species | URL | sha256 |
|---|---|---|
| human | `https://data.broadinstitute.org/gsea-msigdb/msigdb/release/2025.1.Hs/h.all.v2025.1.Hs.symbols.gmt` | `f22066af72e215ccb7b89d88e492c07e1eef17534c2ca7b0f9902cfecbbdd8e9` |
| mouse | `https://data.broadinstitute.org/gsea-msigdb/msigdb/release/2025.1.Mm/mh.all.v2025.1.Mm.symbols.gmt` | `2909a73db35f66a538267fa88dd8260e864a5c8d84d3c49c5262fa6bdb6e0805` |

Each file holds the 50 Hallmark sets as gene symbols. Fetch it with the checksum
(`bench.fetch(url, sha256=...)`). Other collections (C2 pathways, C5 GO) are in the
same release folder; the dataset's own gene ids must match (symbols vs Ensembl ids).
MSigDB is CC BY 4.0, but some C2 sets (KEGG) carry their own terms: name the
collection and its license in the cell's `why`.

## Scoring (decoupler 2)

decoupler is in sc-hub's environment; an environment built before it was added lacks it:
if `import decoupler` fails, `bench.packages(pip=["decoupler>=2"])` and run the next cell.

```
import decoupler as dc
net = dc.pp.read_gmt(path)                    # columns: source (set), target (gene)
dc.mt.ulm(adata, net, tmin=5)                 # per cell or per pseudobulk sample
scores = adata.obsm["score_ulm"]              # samples x sets; adata.obsm["padj_ulm"]
scores, padj = dc.mt.ulm(de_stats, net)       # a DataFrame: one row (the contrast), genes as columns (e.g. DE t-stats)
```

- Score normalized, log-transformed data (or a DE statistic), not raw counts.
- Membership comes from the GMT file, never from memory: write which sets and which
  release in the cell's `why`.
- Say how many genes of each set are in the data; a set with few measured genes
  (tmin) is dropped, which is itself a result worth noting.
- Per-cell scores of donors or samples are not independent replicates: compare
  conditions on pseudobulk samples (`dc.pp.pseudobulk`) or per donor.
- Scores per cluster or condition are descriptive until a registered test decides
  something (skills("rigor")).
