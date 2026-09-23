"""Small HTML helpers; every piece of text passes through `esc`."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Iterable

SKIP_SUMMARY_KEYS = {"groups", "figure", "warnings"}


def esc(value: Any) -> str:
    return escape(str(value), quote=True)


def pill(state: str, label: str | None = None) -> str:
    return f'<span class="pill {esc(state)}">{esc((label or state).lower())}</span>'


def dot(state: str, title: str = "") -> str:
    return f'<span class="dot {esc(state)}" title="{esc(title or state.lower())}"></span>'


def table(headers: Iterable[str], rows: Iterable[str], css: str = "") -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(rows)
    return f'<table class="{esc(css)}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _short(value: Any) -> str:
    if isinstance(value, (str, int, float)) or value is None:
        text = str(value)
    else:
        text = json.dumps(value, default=str)
    return text if len(text) <= 160 else text[:157] + "..."


def kv(data: dict[str, Any], skip: set[str] = SKIP_SUMMARY_KEYS) -> str:
    rows = [
        f"<tr><th>{esc(k)}</th><td>{esc(_short(v))}</td></tr>"
        for k, v in data.items() if k not in skip
    ]
    return f'<table class="kv">{"".join(rows)}</table>' if rows else ""


def warnings(summary: dict[str, Any] | None) -> str:
    items = (summary or {}).get("warnings") or []
    return "".join(f'<p class="note warn">{esc(w)}</p>' for w in items if isinstance(w, str))
