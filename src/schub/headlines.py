"""One-line human summaries of step results, shared by the logbook and dashboard."""

from __future__ import annotations

from typing import Any, Callable, Mapping


def _qc(s: Mapping[str, Any]) -> str:
    return f"{s.get('cells_final', '?'):,} cells kept" if isinstance(s.get("cells_final"), int) else ""


def _normalize(s: Mapping[str, Any]) -> str:
    return f"{s.get('n_clusters', '?')} clusters"


def _scvi(s: Mapping[str, Any]) -> str:
    device = "GPU" if "cpu" not in str(s.get("device", "")).lower() else "CPU"
    return f"{s.get('epochs', '?')} epochs, {device}"


def _celltypist(s: Mapping[str, Any]) -> str:
    purity = s.get("reference_purity")
    if isinstance(purity, (int, float)):
        return f"{s.get('n_labels', '?')} labels, {purity:.1%} consistent with {s.get('reference_key')}"
    return f"{s.get('n_labels', '?')} labels"


def _per_group(s: Mapping[str, Any], field: str) -> list[int]:
    groups = s.get("groups", {})
    return [g[field] for g in groups.values() if isinstance(g, dict) and isinstance(g.get(field), int)]


def _de(s: Mapping[str, Any]) -> str:
    """Genes, not gene x group pairs: a gene up in 7 cell types is one DE gene."""
    tested = s.get("groups_tested", "?")
    unique = s.get("significant_genes")
    if isinstance(unique, int):
        return f"{unique:,} DE genes in {tested} groups"
    counts = _per_group(s, "significant")  # older results: say per group, never the sum
    if len(counts) > 1:
        return f"{tested} groups, {min(counts):,}–{max(counts):,} DE genes each"
    return f"{counts[0]:,} DE genes" if counts else f"{tested} groups"


def _counted(s: Mapping[str, Any]) -> str:
    cells = s.get("n_cells")
    return f"{cells:,} cells from {len(s.get('samples', {}))} sample(s)" if isinstance(cells, int) else ""


def _scanvi(s: Mapping[str, Any]) -> str:
    """Accuracy on held-out labels; agreement with the training labels is not a check."""
    labels = s.get("n_labels", "?")
    accuracy = s.get("holdout_accuracy")
    if isinstance(accuracy, (int, float)):
        return f"{labels} labels, {accuracy:.1%} held-out accuracy"
    unlabeled = s.get("unlabeled_cells")
    if unlabeled == 0:
        return f"{labels} labels (every cell labeled, no check)"
    return f"{labels} labels, {unlabeled:,} cells predicted" if isinstance(unlabeled, int) else f"{labels} labels"


def _memento(s: Mapping[str, Any]) -> str:
    mean, var = s.get("significant_genes"), s.get("variability_genes")
    if isinstance(mean, int) and isinstance(var, int):
        return f"{mean:,} mean / {var:,} variability genes"
    means, variability = _per_group(s, "significant"), _per_group(s, "variability_significant")
    if len(means) == 1:
        return f"{means[0]:,} mean / {sum(variability):,} variability genes"
    return f"{s.get('groups_tested', len(means))} groups, up to {max(means, default=0):,} mean / " \
           f"{max(variability, default=0):,} variability genes"


def _export(s: Mapping[str, Any]) -> str:
    return f"{s.get('n_cells', '?'):,} cells, {s.get('size_mb', '?')} MB" if isinstance(s.get("n_cells"), int) else ""


def _merged(s: Mapping[str, Any]) -> str:
    cells = s.get("n_cells")
    return f"{len(s.get('datasets', {}))} datasets, {cells:,} cells" if isinstance(cells, int) else ""


HEADLINES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "merge_datasets": _merged,
    "kb_count": _counted,
    "cellranger_count": _counted,
    "integrate_scanvi": _scanvi,
    "memento_de": _memento,
    "export_cellxgene": _export,
    "qc_filter": _qc,
    "normalize_embed": _normalize,
    "integrate_scvi": _scvi,
    "annotate_celltypist": _celltypist,
    "pseudobulk_de": _de,
}


def headline(brick: str, summary: Mapping[str, Any] | None) -> str:
    if not summary:
        return ""
    try:
        return HEADLINES.get(brick, lambda _s: "")(summary)
    except (TypeError, ValueError, AttributeError):
        return ""


def duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else (f"{minutes}m {secs:02d}s" if minutes else f"{secs}s")
