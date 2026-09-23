from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from ...aggregate import pseudobulk
from ..base import BrickError, StepIO
from ..pseudobulk_de import PseudobulkParams
from .common import counts_source, read

SEP = "\x1f"
MIN_GENE_COUNT = 10


@dataclass(frozen=True)
class GroupResult:
    summary: dict[str, Any]
    table: Any | None


def _sample_table(counts: Any, cond: Any, rep: Any, var_names: Any, p: PseudobulkParams) -> tuple[Any, Any]:
    import numpy as np
    import pandas as pd

    samples, summed, n_cells = pseudobulk(counts, (rep + SEP + cond).to_numpy())
    meta = pd.DataFrame([s.split(SEP, 1) for s in samples], columns=["replicate", "condition"], index=samples)
    meta["n_cells"] = n_cells
    keep = (meta["n_cells"] >= p.min_cells).to_numpy()
    frame = pd.DataFrame(np.rint(summed[keep]).astype(np.int64), index=meta.index[keep], columns=var_names)
    return meta[keep], frame.loc[:, frame.sum(axis=0) >= MIN_GENE_COUNT]


def _test_group(counts: Any, cond: Any, rep: Any, var_names: Any, p: PseudobulkParams, inference: Any) -> GroupResult:
    import pandas as pd
    from pydeseq2.dds import DeseqDataSet
    from pydeseq2.ds import DeseqStats

    meta, frame = _sample_table(counts, cond, rep, var_names, p)
    dropped: list[str] = []
    if p.paired:
        # A replicate seen in only one condition (in this group) cannot be paired.
        per_replicate = meta.groupby("replicate")["condition"].nunique()
        dropped = sorted(per_replicate[per_replicate < 2].index)
        keep = ~meta["replicate"].isin(dropped).to_numpy()
        meta, frame = meta[keep], frame[keep]
    per_condition = meta.groupby("condition")["replicate"].nunique().to_dict()
    short = {lvl: per_condition.get(lvl, 0) for lvl in (p.reference, p.treatment)}
    if min(short.values()) < p.min_replicates:
        reason = f"replicates with >= {p.min_cells} cells: {short}"
        return GroupResult({"skipped": reason, "dropped_unpaired": dropped}, None)
    design = "~replicate + condition" if p.paired else "~condition"
    metadata = pd.DataFrame(
        {
            "replicate": pd.Categorical(meta["replicate"]),
            "condition": pd.Categorical(meta["condition"], categories=[p.reference, p.treatment]),
        },
        index=meta.index,
    )
    dds = DeseqDataSet(counts=frame, metadata=metadata, design=design, inference=inference, quiet=True)
    dds.deseq2()
    stats = DeseqStats(
        dds, contrast=["condition", p.treatment, p.reference], alpha=p.alpha, inference=inference, quiet=True
    )
    stats.summary()
    table = stats.results_df.sort_values("padj")
    significant = table[table["padj"] < p.alpha]
    return GroupResult(
        {
            "design": design,
            "samples": int(len(meta)),
            "replicates": short,
            "dropped_unpaired": dropped,
            "genes_tested": int(frame.shape[1]),
            "significant": int(len(significant)),
            "top_up": significant[significant["log2FoldChange"] > 0].head(5).index.tolist(),
            "top_down": significant[significant["log2FoldChange"] < 0].head(5).index.tolist(),
        },
        table,
    )


def _write_csv(frame: Any, path: Any) -> None:
    """Write next to the target and rename: the dashboard never reads half a table."""
    partial = path.with_name(f".{path.name}.partial")
    frame.to_csv(partial)
    os.replace(partial, path)


def run(io: StepIO, p: PseudobulkParams) -> dict[str, Any]:
    import pandas as pd
    from pydeseq2.default_inference import DefaultInference

    adata = read(io)
    counts, _ = counts_source(adata, io)
    obs = adata.obs
    cond = obs[p.condition_key].astype(str)
    rep = obs[p.replicate_key].astype(str)
    groups = obs[p.group_key].astype(str) if p.group_key else pd.Series("all", index=obs.index)
    selected = cond.isin([p.reference, p.treatment]).to_numpy()
    if not selected.any():
        raise BrickError(f"No cells with '{p.condition_key}' in {[p.reference, p.treatment]}.")
    inference = DefaultInference(n_cpus=int(os.environ.get("OMP_NUM_THREADS", "4")))
    summaries: dict[str, Any] = {}
    tables = []
    for group in sorted(groups[selected].unique()):
        mask = selected & (groups == group).to_numpy()
        result = _test_group(counts[mask], cond[mask], rep[mask], adata.var_names, p, inference)
        summaries[group] = result.summary
        if result.table is not None:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", group)[:60]
            _write_csv(result.table, io.results_dir / f"de_{safe}.csv")
            tables.append(result.table.assign(group=group))
    if not tables:
        raise BrickError(f"No group had enough replicates per condition: {summaries}")
    combined = pd.concat(tables)
    _write_csv(combined, io.results_dir / "de_all.csv")
    return {
        "contrast": f"{p.treatment} vs {p.reference}",
        # A gene significant in several cell types counts once here, once per group below.
        "significant_genes": int(combined.index[combined["padj"] < p.alpha].unique().size),
        "groups_tested": len(tables),
        "groups_skipped": len(summaries) - len(tables),
        "groups": summaries,
    }
