"""The Journal tab: what the assistant did and found, one card per cell or note,
newest first. Calm by default: why, expected, the result and its checks; code,
files and logs fold away; decisions and mistakes sit behind the '⋯' menu."""

from __future__ import annotations

from typing import Any

from .collect_journal import BenchPanel, JournalCard
from .html import esc, hint, menu, pill

OUTPUT_LINES = 14
STATUS_CSS = {"ok": "COMPLETED", "error": "FAILED", "lost": "FAILED", "retired": "CANCELLED",
              "interrupted": "CANCELLED", "running": "RUNNING", "queued": "PENDING"}
NOTE_LABELS = {"registration": "registered", "decision": "decision", "finding": "finding", "error": "mistake",
               "incident": "incident", "verdict": "verdict", "handoff": "hand-over", "note": "note"}
JOURNAL_HINT = ("Every cell your assistant ran, with why it ran and what it expected, and the notes it wrote. "
                "Copy a reference (⋯ on a card) to ask about that cell.")


def _time(created: str) -> str:
    return created[5:16].replace("T", " ")


def _output(entry: dict[str, Any]) -> str:
    parts = []
    for output in entry["outputs"]:
        if output["image"]:
            parts.append(f'<img class="jfig" loading="lazy" alt="figure" src="{esc(output["image"])}">')
        elif output["text"].strip():
            lines = output["text"].rstrip("\n").splitlines()
            head = "\n".join(lines[:OUTPUT_LINES])
            css = "out err" if output["kind"] == "error" else "out"
            block = f'<pre class="{css}">{esc(head)}</pre>'
            if len(lines) > OUTPUT_LINES:
                block = (f'<details class="more-out"><summary>{block}<span class="muted small">'
                         f'{len(lines) - OUTPUT_LINES} more lines</span></summary>'
                         f'<pre class="{css}">{esc(chr(10).join(lines[OUTPUT_LINES:]))}</pre></details>')
            parts.append(block)
    return "".join(parts)


def _badges(entry: dict[str, Any]) -> str:
    badges = [pill("COMPLETED" if c["status"] == "pass" else "FAILED", f"check {c['name']}") for c in entry["checks"]]
    badges += [pill(j["state"] if j["state"] in ("COMPLETED", "FAILED", "RUNNING", "PENDING") else "CANCELLED",
                    f"job {j['job_id']} {j['state'].lower()}") for j in entry["jobs"]]
    if entry["data_scope"] in ("twin", "full"):
        badges.append(f'<span class="tag">{esc(entry["data_scope"])} data</span>')
    if entry["setup"]:
        badges.append('<span class="tag">setup</span>')
    badges += [f'<span class="tag" title="{esc(b)}">brick {esc(b.split()[0])}</span>' for b in entry.get("bricks", [])]
    return f'<div class="badges">{"".join(badges)}</div>' if badges else ""


def _download_row(d: dict[str, Any]) -> str:
    if d.get("commit"):
        return (f"<li>cloned <code>{esc(d['url'])}</code> <span class='muted small'>commit "
                f"{esc(d['commit'][:12])}</span></li>")
    return f"<li>downloaded <code>{esc(d['url'])}</code> <span class='muted small'>sha256 {esc(d['sha256'][:12])}</span></li>"


def _folded(entry: dict[str, Any]) -> str:
    parts = [f'<details class="fold"><summary>code</summary><pre class="code">{esc(entry["code"])}</pre></details>']
    if entry["files"] or entry["downloads"]:
        rows = [f"<li>{esc(f['change'])} <code>{esc(f['path'])}</code></li>" for f in entry["files"]]
        rows += [_download_row(d) for d in entry["downloads"]]
        label = f"{len(entry['files'])} file(s)" + (f", {len(entry['downloads'])} download(s)" if entry["downloads"] else "")
        parts.append(f'<details class="fold"><summary>{esc(label)}</summary><ul>{"".join(rows)}</ul></details>')
    failed = [c for c in entry["checks"] if c["status"] != "pass"]
    if failed:
        parts.append("".join(f'<p class="note warn small">{esc(c["name"])}: {esc(c["message"])}</p>' for c in failed))
    return "".join(parts)


def _foot(entry: dict[str, Any]) -> str:
    engine = f' <span class="engine" title="the lab agent\'s engine">{esc(entry["engine"])}</span>' if entry["engine"] else ""
    return f'<div class="jfoot muted small"><code>{esc(entry["ref"])}</code> · {esc(entry["by"])}{engine}</div>'


def _engine_filter(card: JournalCard) -> str:
    engines = sorted({e["engine"] for e in card.entries if e.get("engine")})
    if not engines:
        return ""
    buttons = "".join(f'<button type="button" data-engine-filter="{esc(e)}">{esc(e)}</button>' for e in engines)
    return (f'<p class="pop-label">Lab agent\'s entries</p><div class="filters">'
            f'<button type="button" data-engine-filter="">all</button>{buttons}</div>')


def _cell_card(entry: dict[str, Any]) -> str:
    state = STATUS_CSS.get(entry["status"], "PENDING")
    took = f" · {entry['duration_s']:.0f} s" if entry.get("duration_s") else ""
    actions = menu(f'<button type="button" data-copy="{esc(entry["ref"])}">Copy reference</button>', "Cell actions")
    message = f'<p class="note small">{esc(entry["message"])}</p>' if entry["message"] else ""
    return (
        f'<article class="jcard" data-status="{esc(entry["status"])}" data-by="{esc(entry["by"])}"'
        f' data-engine="{esc(entry["engine"])}">'
        f'<div class="jhead">{pill(state, entry["status"])}<b class="why">{esc(entry["why"])}</b>'
        f'<span class="muted small right">{esc(_time(entry["created"]))}{esc(took)}</span>{actions}</div>'
        f'<div class="muted small expect">expected: {esc(entry["expect"])}</div>{_badges(entry)}{message}'
        f'{_output(entry)}{_folded(entry)}'
        f'{_foot(entry)}</article>'
    )


def _note_card(entry: dict[str, Any]) -> str:
    label = NOTE_LABELS.get(entry["kind"], entry["kind"])
    because = f'<div class="muted small">because {esc(", ".join(entry["because"]))}</div>' if entry["because"] else ""
    reverses = f'<div class="muted small">reversed if: {esc(entry["reverses_if"])}</div>' if entry["reverses_if"] else ""
    numbers = (f'<p class="note warn small">numbers not found in the cited cells: '
               f'{esc(", ".join(entry["unresolved_numbers"]))}</p>' if entry["unresolved_numbers"] else "")
    audience = '<span class="tag">for you</span>' if entry["audience"] == "human" else ""
    return (
        f'<article class="jcard note-card" data-kind="{esc(entry["kind"])}" data-by="{esc(entry["by"])}"'
        f' data-engine="{esc(entry["engine"])}">'
        f'<div class="jhead"><span class="tag kind">{esc(label)}</span>{audience}'
        f'<span class="muted small right">{esc(_time(entry["created"]))}</span></div>'
        f'<p class="note-text">{esc(entry["text"])}</p>{because}{reverses}{numbers}'
        f'{_foot(entry)}</article>'
    )


def _decisions(card: JournalCard) -> str:
    rows = [f"<tr><td>{esc(e['text'][:160])}</td><td>{esc(', '.join(e['because']))}</td>"
            f"<td>{esc(e['reverses_if'])}</td></tr>" for e in card.entries if e["kind"] == "decision"]
    if not rows:
        return '<p class="muted small">No decisions recorded yet.</p>'
    return ('<table class="small"><thead><tr><th>Decision</th><th>Because</th><th>Reversed if</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _mistakes(card: JournalCard) -> str:
    items = [f"<li>{esc(e['ref'])}: {esc(e['text'][:200])}</li>" for e in card.entries if e["kind"] in ("error", "incident")]
    items += [f"<li>{esc(e['ref'])} {esc(e['why'][:120])}: {esc(e['status'])} {esc(e['message'][:160])}</li>"
              for e in card.entries if e["kind"] == "cell" and e["status"] in ("error", "lost", "retired")]
    return f'<ul class="small">{"".join(items)}</ul>' if items else '<p class="muted small">Nothing went wrong yet.</p>'


def _report_line(card: JournalCard) -> str:
    """The newest published report: its page, its notebook, and whether the journal went on since."""
    if not card.reports:
        return ""
    latest = card.reports[-1]
    title = esc(latest["title"])
    page = (f'<a href="{esc(latest["view"])}/report.html" target="_blank" rel="noopener">{title}</a>'
            if latest["html"] else f"<span>{title}</span>")
    name = f'{card.project.replace("/", ".")}.{latest["folder"].removeprefix("reports/")}'
    notebook = (f'<button type="button" class="link" data-jnb="{esc(report_key(card.project, latest["folder"]))}" '
                f'data-src="{esc(latest["view"])}/report.js" data-name="{esc(name)}">notebook</button>')
    newer = latest["newer"]
    after = f' <span class="muted">· {newer} newer cell{"" if newer == 1 else "s"} since</span>' if newer else ""
    earlier = f' <span class="muted">· {len(card.reports) - 1} earlier</span>' if len(card.reports) > 1 else ""
    return f'<p class="jreport small">Report: {page} · {notebook}{after}{earlier}</p>'


def report_key(project: str, folder: str) -> str:
    return f"report:{project}:{folder}"


def _project(card: JournalCard) -> str:
    entries = "".join(_cell_card(e) if e["kind"] == "cell" else _note_card(e) for e in reversed(card.entries))
    handoff = (f'<details class="fold handoff"><summary>Hand-over</summary><pre class="out">{esc(card.handoff)}</pre>'
               f'</details>' if card.handoff.strip() else "")
    nxt = f'<span class="muted">next: {esc(card.next_action)}</span>' if card.next_action else ""
    more = menu(f'{_engine_filter(card)}<p class="pop-label">Decisions</p>{_decisions(card)}'
                f'<p class="pop-label">What went wrong</p>'
                f'{_mistakes(card)}<button type="button" data-jnb="{esc(card.project)}" '
                f'data-src="{esc(card.notebook)}.js">Download as a notebook</button>',
                "Decisions, mistakes, notebook", end=False)  # the ⋯ sits left, after the title: open rightwards
    return (
        f'<section class="journal" data-journal="{esc(card.project)}" hidden>'
        f'<div class="jtitle"><h2>{esc(card.project)}</h2>{more}</div>'
        f'<p class="question">{esc(card.question)}</p>{_report_line(card)}'
        f'<div class="jstate">{pill("RUNNING" if card.disposition == "active" else "PENDING", card.disposition)}{nxt}</div>'
        f'{handoff}<div class="jcards">{entries or "<p class=empty>No cells yet.</p>"}</div></section>'
    )


def render_journal(cards: tuple[JournalCard, ...], panel: BenchPanel | None) -> str:
    alerts = "".join(f'<p class="note warn small">{esc(a)}</p>' for a in (panel.alerts if panel else ()))
    state = f'<p class="muted small">{esc(panel.workbench)}</p>' if panel else ""
    if not cards:
        return (f'{alerts}<div class="empty">No bench work yet. Ask your assistant, e.g. "Create a project for my '
                f'question and load the Kang 2018 dataset".</div>{state}')
    tree = "".join(f'<button type="button" data-journal-link="{esc(c.project)}">{esc(c.project)}'
                   f'<span class="count">{c.cells}</span></button>' for c in cards)
    head = f'<div class="jbar"><nav class="journal-tree">{tree}</nav>{hint(JOURNAL_HINT, end=True)}</div>'
    return f'{alerts}{head}{state}{"".join(_project(c) for c in cards)}'


JOURNAL_SCRIPT = r"""
(() => {
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  function keep(k, v) { try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch (e) { return null; } }
  // The notebook comes as jnb/<project>.js: scripts load on file:// pages, fetch does not.
  function download(button) {
    const name = button.dataset.jnb, have = () => window.SCHUB_JNB && window.SCHUB_JNB[name];
    const save = data => {
      if (!data) { button.textContent = 'Notebook unavailable'; return; }
      const blob = new Blob([JSON.stringify(data, null, 1)], {type: 'application/x-ipynb+json'});
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = (button.dataset.name || name.replace(/\//g, '.')) + '.ipynb';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    };
    if (have()) return save(have());
    const tag = document.createElement('script');
    tag.src = button.dataset.src; tag.onload = () => save(have()); tag.onerror = () => save(null);
    document.head.appendChild(tag);
  }
  window.SCHUB_JOURNAL = {
    select(path) {
      const sections = $$('.journal[data-journal]');
      if (!sections.length) return;
      if (!sections.some(s => s.dataset.journal === path)) path = keep('journal') || sections[0].dataset.journal;
      if (!sections.some(s => s.dataset.journal === path)) path = sections[0].dataset.journal;
      sections.forEach(s => { s.hidden = s.dataset.journal !== path; });
      $$('[data-journal-link]').forEach(b => b.classList.toggle('active', b.dataset.journalLink === path));
      keep('journal', path);
    },
  };
  document.addEventListener('click', e => {
    const link = e.target.closest('[data-journal-link]');
    if (link) { location.hash = 'journal/' + link.dataset.journalLink; return; }
    const nb = e.target.closest('[data-jnb]');
    if (nb) { download(nb); return; }
    const only = e.target.closest('[data-engine-filter]');
    if (only) {
      const section = only.closest('.journal'), value = only.dataset.engineFilter;
      $$('.jcard', section).forEach(c => { c.hidden = Boolean(value) && c.dataset.engine !== value; });
      $$('[data-engine-filter]', section).forEach(b => b.classList.toggle('active', b === only));
      return;
    }
    const copy = e.target.closest('[data-copy]');
    if (copy) {
      const text = copy.dataset.copy;
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
        () => { copy.textContent = 'Copied ' + text; }, () => { window.prompt('Copy this reference', text); });
    }
  });
})();
"""
