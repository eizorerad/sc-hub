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


def hint(text: str, end: bool = False) -> str:
    """A small '?' that explains on hover or focus, in place of a paragraph on the page."""
    return hint_html(esc(text), text, end)


def hint_html(body: str, label: str, end: bool = False) -> str:
    """hint() with markup inside (e.g. the colour legend); body must be escaped already."""
    return (f'<span class="hint{" end" if end else ""}" tabindex="0" role="button" aria-label="{esc(label)}">'
            f'<span class="hint-mark" aria-hidden="true">?</span><span class="tip" role="tooltip">{body}</span></span>')


def menu(items: str, label: str = "More", end: bool = True) -> str:
    """A '⋯' button that opens secondary actions and details (closes on an outside click)."""
    return (f'<details class="menu-pop{" end" if end else ""}"><summary class="dots" title="{esc(label)}" '
            f'aria-label="{esc(label)}"><span aria-hidden="true">⋯</span></summary><div class="pop">{items}</div></details>')


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


ASK_HINT = ("The button copies a ready request: paste it into Codex or Claude and replace the part in <…>. "
            "A fix makes a new revision of the same branch (r2, r3…); an alternative from this step on is a "
            "new branch, the old one stays as it is.")


def ask_block(ref: str, brick: str, branch: str, note: str = "", refs: tuple[str, ...] = ()) -> str:
    """Buttons that copy a ready request for Codex / Claude about one step: fix it in
    place (a new revision of the branch) or try an alternative from here (a new
    branch). The page script writes the text from these attributes on click. With
    several branches using the step, a picker chooses which one the request is about:
    the page sets it to the branch on screen; on a project map the student picks it."""
    choices = refs if len(refs) > 1 else ()
    picker = '<option value="">which branch?</option>' + "".join(
        f'<option value="{esc(r)}">{esc(r.rpartition("#")[0])}</option>' for r in choices)
    target = (f'<select class="ask-ref" aria-label="Which branch">{picker}</select>' if choices
              else f'<code class="ref">{esc(ref)}</code>')
    warning = f'<p class="note warn small">{esc(note)}</p>' if note else ""  # stays visible: it changes the request
    return (
        f'<div class="ask" data-ref="{esc(ref)}" data-brick="{esc(brick)}" data-branch="{esc(branch)}">'
        f'<div class="ask-head"><b>Ask your assistant</b>{hint(ASK_HINT, end=True)}</div>{warning}'
        '<div class="ask-buttons"><button type="button" class="primary" data-ask="fix">Fix this step</button>'
        '<button type="button" data-ask="fork">Try an alternative</button></div>'
        f'<div class="ask-foot">{target}<button type="button" data-ask="ref" class="link">copy reference</button></div>'
        '<p class="muted small copied" hidden>Copied: paste it into Codex or Claude.</p>'
        '<textarea class="manual" readonly hidden rows="4" aria-label="Request to copy"></textarea></div>'
    )
