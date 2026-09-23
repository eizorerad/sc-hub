"""Small HTML helpers; every piece of text passes through `esc`."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Iterable

SKIP_SUMMARY_KEYS = {"groups", "figure", "warnings", "by_reference"}  # shown as their own tables


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


def listing(headers: Iterable[str], rows: list[tuple[str, str]], placeholder: str, limit: int = 25, css: str = "",
            key: str = "") -> str:
    """A table that stays usable at any length: a filter box and 'Show all N' appear
    once there are more rows than fit. rows: (search text, '<td>...</td>' cells)."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(f'<tr data-row data-text="{esc(text.lower())}">{cells}</tr>' for text, cells in rows)
    many = len(rows) > min(limit, 8)
    search = f'<input class="list-filter" type="search" placeholder="{esc(placeholder)}" aria-label="{esc(placeholder)}">' if many else ""
    more = '<button class="more" type="button" hidden></button>' if len(rows) > limit else ""
    return (f'<div class="listing" data-limit="{limit}" data-key="{esc(key or placeholder)}">{search}'
            f'<table class="{esc(css)}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}</div>')


def subtabs(group: str, sections: list[tuple[str, str, int | None, str]]) -> str:
    """Sections of one view behind pills, so a long first section never buries the
    others. sections: (id, label, count or None, html)."""
    buttons = "".join(
        f'<button type="button" data-sub="{esc(sid)}">{esc(label)}'
        f'{f"<span class=count>{count}</span>" if count is not None else ""}</button>'
        for sid, label, count, _ in sections
    )
    views = "".join(f'<div class="subview" data-subview="{esc(group)}" data-sub="{esc(sid)}">{html}</div>'
                    for sid, _, _, html in sections)
    return f'<div class="subtabs" data-subtabs="{esc(group)}">{buttons}</div>{views}'


def ask_block(ref: str, brick: str, branch: str, note: str = "") -> str:
    """Buttons that copy a ready request for Codex / Claude about one step: fix it in
    place (a new revision of the branch) or try an alternative from here (a new
    branch). The page script writes the text from these attributes on click."""
    return (
        f'<div class="ask" data-ref="{esc(ref)}" data-brick="{esc(brick)}" data-branch="{esc(branch)}">'
        f'<div class="ask-head"><span class="muted small">Ask your assistant about</span> <code class="ref">{esc(ref)}</code></div>'
        '<div class="ask-buttons"><button type="button" data-ask="fix">Fix this step</button>'
        '<button type="button" data-ask="fork">Try an alternative from here</button>'
        '<button type="button" data-ask="ref" class="quiet">Copy reference</button></div>'
        + (f'<p class="muted small">{esc(note)}</p>' if note else "")
        + '<p class="muted small copied" hidden>Copied: paste it into Codex or Claude and replace the part in &lt;…&gt;.</p>'
        '<textarea class="manual" readonly hidden rows="4" aria-label="Request to copy"></textarea></div>'
    )
