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


HEADLINES: dict[str, Callable[[Mapping[str, Any]], str]] = {
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
