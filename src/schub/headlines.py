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
    return f"{s.get('n_labels', '?')} labels"


def _de(s: Mapping[str, Any]) -> str:
    groups = s.get("groups", {})
    significant = sum(g.get("significant", 0) for g in groups.values() if isinstance(g, dict))
    return f"{s.get('groups_tested', '?')} groups, {significant:,} DE genes"


def _counted(s: Mapping[str, Any]) -> str:
    cells = s.get("n_cells")
    return f"{cells:,} cells from {len(s.get('samples', {}))} sample(s)" if isinstance(cells, int) else ""


def _scanvi(s: Mapping[str, Any]) -> str:
    return f"{s.get('n_labels', '?')} labels, {s.get('label_agreement_on_labeled', '?')} agreement"


def _memento(s: Mapping[str, Any]) -> str:
    groups = [g for g in s.get("groups", {}).values() if isinstance(g, dict)]
    mean = sum(g.get("significant", 0) for g in groups)
    var = sum(g.get("variability_significant", 0) for g in groups)
    return f"{mean:,} mean / {var:,} variability genes"


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
