from __future__ import annotations

import os
import re
from typing import Any

from ..base import BrickError, StepIO
from ..memento_de import MementoParams
from .common import counts_source, read, setup_scanpy
from .pseudobulk_de import _write_csv

TREATMENT = "_schub_treatment"


def _test(adata: Any, p: MementoParams, cpus: int) -> Any:
    import memento
    from statsmodels.stats.multitest import multipletests

    replicates = [p.replicate_key] if p.replicate_key else []
    table = memento.binary_test_1d(
        adata, capture_rate=p.capture_rate, treatment_col=TREATMENT, num_cpus=cpus,
        num_boot=p.num_boot, verbose=0, replicates=replicates,
    )
    table = table.drop(columns=["tx"], errors="ignore").set_index("gene")
    for kind in ("de", "dv"):
        pvals = table[f"{kind}_pval"].fillna(1.0)
        table[f"{kind}_padj"] = multipletests(pvals, method="fdr_bh")[1]
    return table.sort_values("de_padj")


def _summary(table: Any, alpha: float) -> dict[str, Any]:
    mean_hits = table[table["de_padj"] < alpha]
    var_hits = table[table["dv_padj"] < alpha]
    return {
        "genes_tested": int(len(table)),
        "significant": int(len(mean_hits)),
        "variability_significant": int(len(var_hits)),
        "top_up": mean_hits[mean_hits["de_coef"] > 0].index[:8].tolist(),
        "top_down": mean_hits[mean_hits["de_coef"] < 0].index[:8].tolist(),
        "top_variability": var_hits.index[:8].tolist(),
    }


def run(io: StepIO, p: MementoParams) -> dict[str, Any]:
    setup_scanpy(io)
    import anndata as ad
    import pandas as pd
    import scipy.sparse as sp

    source = read(io)
    counts, _ = counts_source(source, io)
    obs = source.obs
    keep = obs[p.condition_key].astype(str).isin([p.reference, p.treatment]).to_numpy()
    if not keep.any():
        raise BrickError(f"No cells with '{p.condition_key}' in {[p.reference, p.treatment]}.")
    # memento wants raw counts in X as CSR; build a slim object with only what it needs.
    columns = [c for c in (p.replicate_key, p.group_key) if c]
    adata = ad.AnnData(sp.csr_matrix(counts[keep], dtype="float32"), obs=obs.loc[keep, columns].copy(), var=source.var[[]].copy())
    adata.obs[TREATMENT] = (obs.loc[keep, p.condition_key].astype(str) == p.treatment).astype(int).to_numpy()
    for column in columns:
        adata.obs[column] = adata.obs[column].astype(str)
    del source, counts
    cpus = int(os.environ.get("OMP_NUM_THREADS", "8"))
    groups = sorted(adata.obs[p.group_key].unique()) if p.group_key else ["all cells"]
    summaries: dict[str, Any] = {}
    tables = []
    for group in groups:
        subset = adata[adata.obs[p.group_key] == group].copy() if p.group_key else adata
        per_condition = subset.obs[TREATMENT].value_counts()
        if len(per_condition) < 2 or per_condition.min() < p.min_cells:
            summaries[group] = {"skipped": f"fewer than {p.min_cells} cells in a condition"}
            continue
        table = _test(subset, p, cpus)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", group)[:60]
        _write_csv(table, io.results_dir / f"memento_{safe}.csv")
        summaries[group] = _summary(table, p.alpha)
        tables.append(table.assign(group=group))
    if not tables:
        raise BrickError(f"No group had {p.min_cells}+ cells in both conditions: {summaries}")
    combined = pd.concat(tables)
    _write_csv(combined, io.results_dir / "memento_all.csv")
    return {
        "contrast": f"{p.treatment} vs {p.reference}",
        # Genes significant in any group, each counted once (the groups below count per group).
        "significant_genes": int(combined.index[combined["de_padj"] < p.alpha].unique().size),
        "variability_genes": int(combined.index[combined["dv_padj"] < p.alpha].unique().size),
        "capture_rate": p.capture_rate,
        "groups_tested": len(tables),
        "groups_skipped": len(summaries) - len(tables),
        "groups": summaries,
    }
