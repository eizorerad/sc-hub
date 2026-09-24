"""The Journal: a navigator of projects and one project page at a time.

The navigator stays usable with a hundred projects: a search box, groups by where each
project stands (needs you, in progress, done, quiet for a week, evaluation runs), and
variants ('project/variant') as a tree under their project. A project's page is its own
file (jproj/<project>.js), loaded when it is picked, so the index stays small.

The page tells the story first: the question and where it stands, the outcome (the
report's summary or the hand-over) with the report, its notebook and the protocol
notebook, the variants, the findings and decisions one line each, then the steps one
line each. Details open on a click; system events (a stopped kernel, a usage limit)
stay hidden unless asked for.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from ..bench.inbox import slug
from .collect_journal import BenchPanel, JournalCard
from .html import esc, hint, pill

OUTPUT_LINES = 14
NOTES_SHOWN = 8
DONE_SHOWN = 12
NAV_GROUPS = (("blocked", "Needs you", True), ("running", "Running now", True), ("working", "Open, nothing running", True),
              ("done", "Done", True), ("idle", "Quiet for a week", False), ("eval", "Evaluation runs", False))
RANK = {"blocked": 0, "running": 1, "working": 2, "done": 3, "idle": 4, "eval": 5}
LIVELY = ("blocked", "running", "working")
STATE = {"active": ("RUNNING", "in progress"), "waiting": ("PENDING", "waiting for jobs"),
         "blocked": ("FAILED", "needs you"), "complete": ("COMPLETED", "complete")}
CELL_ICON = {"ok": ("ok", "✓"), "error": ("bad", "✗"), "lost": ("bad", "✗"), "retired": ("muted", "–"),
             "interrupted": ("muted", "–"), "running": ("run", "●"), "queued": ("muted", "○")}
NOTE_LABELS = {"registration": "registered", "decision": "decision", "finding": "finding", "error": "mistake",
               "incident": "incident", "verdict": "verdict", "handoff": "hand-over", "note": "note"}
STORY_KINDS = ("verdict", "finding", "decision", "registration", "error", "note")
JOURNAL_HINT = ("Your projects: find one by name or question. Variants sit under their project. A project's page "
                "starts with where it stands and its outcome; every step is one line that opens on a click.")


def _time(created: str) -> str:
    return created[5:16].replace("T", " ")


def _line(text: str, limit: int = 220) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---- the navigator ------------------------------------------------------------------------


def _tree(cards: tuple[JournalCard, ...]) -> tuple[dict[str, list[JournalCard]], list[JournalCard]]:
    """Children by parent (the nearest ancestor that is a project), and the top-level projects."""
    names = {c.project for c in cards}
    children: dict[str, list[JournalCard]] = {}
    roots = []
    for card in cards:
        parent = card.project.rpartition("/")[0]
        while parent and parent not in names:
            parent = parent.rpartition("/")[0]
        if parent:
            children.setdefault(parent, []).append(card)
        else:
            roots.append(card)
    return children, roots


def _branch(card: JournalCard, children: dict[str, list[JournalCard]]) -> list[JournalCard]:
    found = [card]
    for child in children.get(card.project, []):
        found += _branch(child, children)
    return found


def _branch_group(card: JournalCard, children: dict[str, list[JournalCard]]) -> str:
    """A project's place in the navigator: the liveliest of its branch (a blocked variant needs you, even
    under an evaluation run)."""
    groups = [c.group for c in _branch(card, children)]
    live = [g for g in groups if g in LIVELY]
    if live:
        return min(live, key=RANK.__getitem__)
    if card.group == "eval":
        return "eval"
    return min((g for g in groups if g != "eval"), key=RANK.__getitem__)


def _latest(card: JournalCard, children: dict[str, list[JournalCard]]) -> str:
    return max(c.updated for c in _branch(card, children))


def page_version(html: str) -> str:
    return hashlib.sha1(html.encode()).hexdigest()[:10]


def _status(card: JournalCard) -> str:
    css, glyph = {"blocked": ("bad", "!"), "done": ("ok", "✓"), "idle": ("muted", "○"), "eval": ("muted", "·"),
                  "running": ("run", "●"), "working": ("run", "○")}.get(card.group, ("muted", "○"))
    if card.live and card.group == "eval":
        css, glyph = "run", "●"
    label = card.live or ("open, nothing running" if card.group == "working"
                          else STATE.get(card.disposition, ("", card.disposition))[1])
    return f'<span class="jst {css}" role="img" title="{esc(label)}" aria-label="{esc(label)}">{glyph}</span>'


def _node(card: JournalCard, children: dict[str, list[JournalCard]], versions: dict[str, str], depth: int,
          extra: bool = False) -> str:
    kids = sorted(children.get(card.project, []), key=lambda c: _latest(c, children), reverse=True)
    name = card.project.rpartition("/")[2] if depth else card.project
    text = " ".join(c.project + " " + c.question for c in _branch(card, children)).lower()
    row = (f'<a class="jitem" href="#journal/{esc(card.project)}" data-path="{esc(card.project)}" '
           f'data-v="{esc(versions.get(card.project, ""))}" title="{esc(card.question)}">{_status(card)}'
           f'<span class="jname">{esc(name)}</span><span class="jwhen" data-when="{esc(card.updated)}"></span></a>')
    inner = ""
    if kids:
        live = any(c.group in LIVELY for k in kids for c in _branch(k, children))
        count = len(_branch(card, children)) - 1
        inner = (f'<details class="jkids"{" open" if live else ""}><summary>{count} variant{"s" if count != 1 else ""}'
                 f'</summary>{"".join(_node(k, children, versions, depth + 1) for k in kids)}</details>')
    return f'<div class="jnode{" extra" if extra else ""}" data-text="{esc(text)}">{row}{inner}</div>'


def _navigator(cards: tuple[JournalCard, ...], versions: dict[str, str]) -> str:
    children, roots = _tree(cards)
    groups = []
    for key, label, is_open in NAV_GROUPS:
        members = sorted((r for r in roots if _branch_group(r, children) == key),
                         key=lambda c: _latest(c, children), reverse=True)
        if not members:
            continue
        nodes = "".join(_node(c, children, versions, 0, extra=key == "done" and i >= DONE_SHOWN)
                        for i, c in enumerate(members))
        more = (f'<button type="button" class="link jmore">Show all {len(members)}</button>'
                if key == "done" and len(members) > DONE_SHOWN else "")
        groups.append(f'<details class="jgroup" data-group="{key}"{" open" if is_open else ""}><summary>{esc(label)}'
                      f'<span class="count">{len(members)}</span></summary>{nodes}{more}</details>')
    return (f'<aside class="jnav" aria-label="Projects"><div class="jnav-head">'
            f'<input type="search" id="jnav-q" placeholder="Find a project" aria-label="Find a project">'
            f'{hint(JOURNAL_HINT)}</div>{"".join(groups)}'
            f'<p class="jnone muted small" hidden>No project matches.</p></aside>')


# ---- a project's page ------------------------------------------------------------------------


def _crumbs(project: str, names: set[str]) -> str:
    parts = project.split("/")
    if len(parts) == 1:
        return ""
    links = [f'<a href="#journal/{esc(path)}">{esc(p)}</a>' if (path := "/".join(parts[: i + 1])) in names
             else esc(p) for i, p in enumerate(parts[:-1])]
    return f'<div class="jp-crumbs muted small">{" / ".join(links)} / {esc(parts[-1])}</div>'


def _head(card: JournalCard, names: set[str]) -> str:
    css, label = STATE.get(card.disposition, ("PENDING", card.disposition))
    if card.disposition == "active":  # "active" only says the work is not finished: say whether anything runs
        css, label = ("RUNNING", "running") if card.live else ("PENDING", "open, nothing running")
    now = f'<span class="jp-live">{esc(card.live)}</span>' if card.live else ""
    nxt = (f'<span class="jp-next" title="{esc(card.next_action)}">Next: {esc(_line(card.next_action, 180))}</span>'
           if card.next_action and card.disposition != "complete" else "")
    meta = f'<span class="muted small">{esc(card.project)} · {card.cells} cells · updated {esc(_time(card.updated))}</span>'
    return (f'{_crumbs(card.project, names)}<h2 class="jp-q">{esc(card.question or card.project)}</h2>'
            f'<div class="jp-state">{pill(css, label)}{now}{nxt}{meta}</div>')


def report_key(project: str, folder: str) -> str:
    return f"report:{project}:{folder}"


def _links(card: JournalCard) -> str:
    links = []
    if card.reports:
        latest = card.reports[-1]
        name = f'{card.project.replace("/", ".")}.{latest["folder"].removeprefix("reports/")}'
        if latest["html"]:
            links.append(f'<a href="{esc(latest["view"])}/report.html" target="_blank" rel="noopener">Open the report</a>')
        links.append(f'<button type="button" class="link" data-jnb="{esc(report_key(card.project, latest["folder"]))}" '
                     f'data-src="{esc(latest["view"])}/report.js" data-name="{esc(name)}">Report notebook</button>')
    links.append(f'<button type="button" class="link" data-jnb="{esc(card.project)}" data-src="{esc(card.notebook)}.js">'
                 'Protocol notebook (every step)</button>')
    notes = []
    if card.reports and card.reports[-1]["newer"]:
        newer = card.reports[-1]["newer"]
        notes.append(f'{newer} newer cell{"" if newer == 1 else "s"} since the report')
    if len(card.reports) > 1:
        notes.append(f"{len(card.reports) - 1} earlier report{'s' if len(card.reports) > 2 else ''} in reports/")
    tail = f'<span class="muted small">{esc(" · ".join(notes))}</span>' if notes else ""
    return f'<div class="jp-links">{"".join(links)}{tail}</div>'


def _outcome(card: JournalCard) -> str:
    label = "Outcome" if card.disposition == "complete" or card.outcome_from == "report" else "Where it stands"
    source = " from the report" if card.outcome_from == "report" else ""
    text = (f'<p class="jp-outcome-text">{esc(card.outcome)}</p>' if card.outcome else
            '<p class="muted small">No hand-over yet: the assistant writes one when it stops.</p>')
    handoff = (f'<details class="fold"><summary>Hand-over</summary><pre class="out">{esc(card.handoff)}</pre></details>'
               if card.handoff.strip() else "")
    return (f'<section class="jp-outcome"><div class="jp-label">{label}<span class="muted">{esc(source)}</span></div>'
            f'{text}{_links(card)}{handoff}</section>')


def _variants(kids: list[JournalCard]) -> str:
    if not kids:
        return ""
    rows = "".join(
        f'<a class="jrow" href="#journal/{esc(k.project)}">{_status(k)}<span class="jrow-name">'
        f'{esc(k.project.rpartition("/")[2])}</span><span class="jrow-text">{esc(_line(k.outcome or k.question, 160))}'
        f'</span><span class="jwhen" data-when="{esc(k.updated)}"></span></a>' for k in kids)
    return (f'<section class="jp-section"><div class="jp-label">Variants<span class="count">{len(kids)}</span></div>'
            f'<div class="jrows">{rows}</div></section>')


def _foot(entry: dict[str, Any]) -> str:
    engine = f' <span class="engine" title="the lab agent\'s engine">{esc(entry["engine"])}</span>' if entry["engine"] else ""
    return (f'<div class="jfoot muted small"><code>{esc(entry["ref"])}</code> · {esc(entry["by"])}{engine} · '
            f'<button type="button" class="link" data-copy="{esc(entry["ref"])}">copy reference</button></div>')


def _note_row(entry: dict[str, Any], extra: bool) -> str:
    label = NOTE_LABELS.get(entry["kind"], entry["kind"])
    because = f'<div class="muted small">because {esc(", ".join(entry["because"]))}</div>' if entry["because"] else ""
    reverses = f'<div class="muted small">reversed if: {esc(entry["reverses_if"])}</div>' if entry["reverses_if"] else ""
    numbers = (f'<p class="note warn small">numbers not found in the cited cells: '
               f'{esc(", ".join(entry["unresolved_numbers"]))}</p>' if entry["unresolved_numbers"] else "")
    flag = '<span class="jmark bad" title="numbers not found in the cited cells">?</span>' \
        if entry["unresolved_numbers"] else ""
    audience = '<span class="tag">for you</span>' if entry["audience"] == "human" else ""
    return (f'<details class="jrow-d{" extra" if extra else ""}" data-kind="{esc(entry["kind"])}" '
            f'data-engine="{esc(entry["engine"])}"><summary><span class="jkind">{esc(label)}</span>'
            f'<span class="jrow-text">{esc(_line(entry["text"]))}</span>{audience}{flag}'
            f'<span class="jwhen-abs">{esc(_time(entry["created"]))}</span></summary>'
            f'<div class="jrow-body"><p class="note-text">{esc(entry["text"])}</p>{because}{reverses}{numbers}'
            f'{_foot(entry)}</div></details>')


def _notes(card: JournalCard) -> str:
    notes = [e for e in reversed(card.entries) if e["kind"] in STORY_KINDS]
    if not notes:
        return ""
    rows = "".join(_note_row(e, i >= NOTES_SHOWN) for i, e in enumerate(notes))
    more = (f'<button type="button" class="link jmore">Show all {len(notes)}</button>' if len(notes) > NOTES_SHOWN
            else "")
    return (f'<section class="jp-section jp-notes"><div class="jp-label">Findings and decisions'
            f'<span class="count">{len(notes)}</span></div><div class="jrows">{rows}</div>{more}</section>')


def _output(entry: dict[str, Any]) -> str:
    parts = []
    for output in entry["outputs"]:
        if output["image"]:
            parts.append(f'<img class="jfig" loading="lazy" alt="figure" src="{esc(output["image"])}">')
        elif output["text"].strip():
            lines = output["text"].rstrip("\n").splitlines()
            css = "out err" if output["kind"] == "error" else "out"
            block = f'<pre class="{css}">{esc(chr(10).join(lines[:OUTPUT_LINES]))}</pre>'
            if len(lines) > OUTPUT_LINES:
                block += (f'<details class="fold"><summary>{len(lines) - OUTPUT_LINES} more lines</summary>'
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


def _failed(entry: dict[str, Any]) -> bool:
    return entry["status"] in ("error", "lost") or any(c["status"] in ("fail", "error") for c in entry["checks"])


def _summary_marks(entry: dict[str, Any]) -> str:
    marks = []
    passed = sum(c["status"] == "pass" for c in entry["checks"])
    failed = len(entry["checks"]) - passed
    if passed:
        marks.append(f'<span class="jmark ok" title="checks passed">✓{passed}</span>')
    if failed:
        marks.append(f'<span class="jmark bad" title="checks failed">✗{failed}</span>')
    for job in entry["jobs"]:
        css = {"COMPLETED": "ok", "FAILED": "bad", "RUNNING": "run"}.get(job["state"], "muted")
        marks.append(f'<span class="jmark {css}" title="job {esc(job["job_id"])} {esc(job["state"].lower())}">job</span>')
    if any(o["image"] for o in entry["outputs"]):
        marks.append('<span class="jmark" title="has a figure">fig</span>')
    if entry["data_scope"] == "twin":
        marks.append('<span class="jmark" title="ran on a twin (a small copy of the data)">twin</span>')
    return f'<span class="jmarks">{"".join(marks)}</span>' if marks else ""


def _step_row(entry: dict[str, Any]) -> str:
    css, glyph = CELL_ICON.get(entry["status"], ("muted", "○"))
    took = f" · {entry['duration_s']:.0f} s" if entry.get("duration_s") else ""
    message = f'<p class="note small">{esc(entry["message"])}</p>' if entry["message"] else ""
    figure = any(o["image"] for o in entry["outputs"])
    return (f'<details class="jrow-d jstep" data-failed="{int(_failed(entry))}" data-fig="{int(figure)}" '
            f'data-engine="{esc(entry["engine"])}"><summary><span class="jst {css}" title="{esc(entry["status"])}">'
            f'{glyph}</span><code class="jcid">{esc(entry["cid"])}</code><span class="jrow-text">{esc(_line(entry["why"]))}'
            f'</span>{_summary_marks(entry)}<span class="jwhen-abs">{esc(_time(entry["created"]))}</span></summary>'
            f'<div class="jrow-body"><div class="muted small">expected: {esc(entry["expect"])}{esc(took)}</div>'
            f'{_badges(entry)}{message}{_output(entry)}{_folded(entry)}{_foot(entry)}</div></details>')


def _system_row(entry: dict[str, Any]) -> str:
    return (f'<details class="jrow-d sys" data-sys="1" hidden><summary><span class="jst muted">·</span>'
            f'<span class="jkind">{esc(NOTE_LABELS.get(entry["kind"], entry["kind"]))}</span>'
            f'<span class="jrow-text">{esc(_line(entry["text"]))}</span><span class="jwhen-abs">'
            f'{esc(_time(entry["created"]))}</span></summary><div class="jrow-body"><p class="note-text">'
            f'{esc(entry["text"])}</p>{_foot(entry)}</div></details>')


def _filters(card: JournalCard, steps: list[dict[str, Any]], system: int) -> str:
    failed = sum(1 for e in steps if _failed(e))
    figures = sum(1 for e in steps if any(o["image"] for o in e["outputs"]))
    engines = sorted({e["engine"] for e in card.entries if e.get("engine")})
    buttons = ['<button type="button" data-step-filter="" class="active">all</button>']
    if failed:
        buttons.append(f'<button type="button" data-step-filter="failed">failed {failed}</button>')
    if figures:
        buttons.append(f'<button type="button" data-step-filter="fig">with figures {figures}</button>')
    buttons += [f'<button type="button" data-step-filter="engine:{esc(e)}" data-engine-filter="{esc(e)}">{esc(e)}</button>'
                for e in engines]
    order = '<button type="button" class="quiet" data-step-order>newest first</button>'
    sys = f'<button type="button" class="quiet" data-step-sys>system events {system}</button>' if system else ""
    return f'<div class="filters">{"".join(buttons)}{order}{sys}</div>'


def _steps(card: JournalCard) -> str:
    steps = [e for e in card.entries if e["kind"] == "cell"]
    system = [e for e in card.entries if e["kind"] == "incident"]
    if not steps and not system:
        return '<p class="empty">No cells yet.</p>'
    rows = "".join(_step_row(e) if e["kind"] == "cell" else _system_row(e) for e in card.entries
                   if e["kind"] in ("cell", "incident"))  # the journal's order: oldest first
    cut = (f'<p class="muted small">Showing the newest {len(card.entries)} of {card.total_entries} entries; the '
           'protocol notebook has all of them.</p>' if card.total_entries > len(card.entries) else "")
    return (f'<section class="jp-section jp-steps"><div class="jp-label">Steps<span class="count">{len(steps)}</span>'
            f'{_filters(card, steps, len(system))}</div>{cut}<div class="jrows jsteps">{rows}</div></section>')


def project_page(card: JournalCard, kids: list[JournalCard], names: set[str] = frozenset()) -> str:
    return (f'<article class="jp" data-project="{esc(card.project)}" data-group="{esc(card.group)}">'
            f'{_head(card, set(names))}'
            f'{_outcome(card)}{_variants(kids)}{_notes(card)}{_steps(card)}</article>')


def project_pages(cards: Iterable[JournalCard]) -> dict[str, str]:
    """Each project's page (HTML), by project."""
    cards = tuple(cards)
    children, _ = _tree(cards)
    names = {c.project for c in cards}
    return {c.project: project_page(c, sorted(children.get(c.project, []), key=lambda k: k.updated, reverse=True),
                                    names) for c in cards}


def page_script(project: str, html: str) -> str:
    """A project's page as a script: scripts load on file:// pages, fetch does not."""
    return (f"window.SCHUB_JPAGE=window.SCHUB_JPAGE||{{}};"
            f"window.SCHUB_JPAGE[{json.dumps(project)}]={json.dumps(html)};\n")


def page_file(project: str) -> str:
    return f"jproj/{slug(project)}.js"


def render_journal(cards: tuple[JournalCard, ...], panel: BenchPanel | None,
                   versions: dict[str, str] | None = None) -> str:
    alerts = "".join(f'<p class="note warn small">{esc(a)}</p>' for a in (panel.alerts if panel else ()))
    state = f'<p class="muted small jbench">{esc(panel.workbench)}</p>' if panel else ""
    if not cards:
        return (f'{alerts}<div class="empty">No bench work yet. Ask your assistant, e.g. "Create a project for my '
                f'question and load the Kang 2018 dataset".</div>{state}')
    toggle = ('<button type="button" class="jnav-toggle" aria-expanded="false">Projects · '
              '<b data-current>pick one</b></button>')
    return (f'{alerts}<div class="jlayout">{toggle}{_navigator(cards, versions or {})}<div class="jmain">{state}'
            f'<div id="jpage" class="jpage"><p class="muted">Pick a project.</p></div></div></div>')


JOURNAL_SCRIPT = r"""
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  function keep(k, v, local) {
    try { const store = local ? localStorage : sessionStorage; if (v === undefined) return store.getItem(k); store.setItem(k, v); }
    catch (e) { return null; }
  }
  // A reload keeps the place: the page script scrolls before a project page has loaded, so the journal
  // restores the position itself, once, and only if the page reloaded on the Journal.
  const onJournal = !location.hash || location.hash.startsWith('#journal');
  let restore = onJournal ? Number(keep('scroll') || 0) : 0;
  let current = null;
  const pages = () => (window.SCHUB_JPAGE = window.SCHUB_JPAGE || {});
  const slug = p => p.replace(/\//g, '.');

  function ago(root) {
    $$('[data-when]', root).forEach(el => {
      const at = Date.parse(el.dataset.when);
      if (!at) return;
      const m = Math.max(0, Math.round((Date.now() - at) / 60000));
      el.textContent = m < 60 ? `${m} min` : m < 48 * 60 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`;
    });
  }
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

  // ---- a project's page ----
  function applySteps(page) {
    const art = $('.jp', page); if (!art) return;
    const key = 'jstep:' + art.dataset.project;
    let filter = keep(key) || '';
    if (!$$('[data-step-filter]', page).some(b => b.dataset.stepFilter === filter)) filter = '';  // not on this page
    const newest = keep('jorder', undefined, true) === 'new', sys = keep('jsys') === 'on';
    $$('[data-step-filter]', page).forEach(b => {
      b.classList.toggle('active', b.dataset.stepFilter === filter); b.setAttribute('aria-pressed', String(b.dataset.stepFilter === filter));
    });
    $$('.jsteps > .jrow-d', page).forEach(row => {
      const s = row.dataset;
      row.hidden = s.sys === '1' ? !(sys && !filter) : !(!filter || (filter === 'failed' && s.failed === '1') ||
        (filter === 'fig' && s.fig === '1') || (filter.startsWith('engine:') && s.engine === filter.slice(7)));
    });
    $$('.jp-notes .jrow-d', page).forEach(row => {
      row.hidden = filter.startsWith('engine:') && row.dataset.engine !== filter.slice(7);
    });
    const steps = $('.jsteps', page);
    if (steps && (steps.dataset.order === 'new') !== newest) {  // the DOM order, so keyboards and readers follow it
      [...steps.children].reverse().forEach(c => steps.appendChild(c));
      steps.dataset.order = newest ? 'new' : 'old';
    }
    const o = $('[data-step-order]', page); if (o) o.textContent = newest ? 'oldest first' : 'newest first';
    const y = $('[data-step-sys]', page); if (y) { y.classList.toggle('active', sys); y.setAttribute('aria-pressed', String(sys)); }
  }
  function show(path) {
    const html = pages()[path];
    if (html === undefined) return false;
    const page = $('#jpage');
    page.innerHTML = html;
    page.style.minHeight = '';
    ago(page); applySteps(page);
    if (restore) { window.scrollTo({top: restore, behavior: 'instant'}); restore = 0; }
    return true;
  }
  function load(item) {
    const path = item.dataset.path;
    if (show(path)) return;
    const page = $('#jpage');
    if (restore) page.style.minHeight = `${restore + window.innerHeight}px`;  // room to come back to
    page.innerHTML = '<p class="muted">Loading…</p>';
    const tag = document.createElement('script');
    tag.src = `jproj/${slug(path)}.js?v=${item.dataset.v || ''}`;
    tag.onload = () => { if (current === path && !show(path)) page.innerHTML = '<p class="muted">This page is empty.</p>'; };
    tag.onerror = () => { if (current === path) page.innerHTML = '<p class="note warn">This project\'s page is missing: refresh the dashboard.</p>'; };
    document.head.appendChild(tag);
  }

  // ---- the navigator ----
  function reveal(item) {
    for (let d = item.closest('details'); d; d = d.parentElement.closest('details')) d.open = true;
    const node = item.closest('.jnode.extra'); if (node) node.closest('.jgroup').classList.add('all');
  }
  function search(q) {
    const nav = $('.jnav'); if (!nav) return;
    q = q.trim().toLowerCase();
    nav.classList.toggle('searching', Boolean(q));
    let any = false;
    $$('.jgroup', nav).forEach(group => {
      let hits = 0;
      $$('.jnode', group).forEach(node => {
        const hit = !q || node.dataset.text.includes(q);
        node.hidden = !hit;
        if (q && hit) $$(':scope > .jkids', node).forEach(d => { d.open = true; });
        if (hit && node.parentElement === group) hits += 1;
      });
      group.hidden = hits === 0;
      if (q && hits) group.open = true;
      any = any || hits > 0;
    });
    const none = $('.jnone', nav); if (none) none.hidden = any;
  }
  // What the student opened in the navigator survives the minute's reload.
  const folded = d => d.matches('.jgroup') ? 'jg:' + d.dataset.group : 'jk:' + d.parentElement.querySelector('.jitem').dataset.path;
  $$('.jgroup, .jkids').forEach(d => { const v = keep(folded(d)); if (v) d.open = v === 'open'; });
  $$('.jgroup').forEach(g => { if (keep('jall:' + g.dataset.group)) { g.classList.add('all'); g.querySelector('.jmore')?.remove(); } });
  document.addEventListener('toggle', e => {
    if (e.target.matches && e.target.matches('.jgroup, .jkids') && !$('.jnav.searching')) keep(folded(e.target), e.target.open ? 'open' : 'closed');
  }, true);

  window.SCHUB_JOURNAL = {
    select(path) {
      const items = $$('.jitem[data-path]');
      if (!items.length) return;
      let item = items.find(i => i.dataset.path === path) || items.find(i => i.dataset.path === keep('journal'));
      item = item || $('.jgroup[open] .jitem') || items[0];
      if (path && item.dataset.path !== path) history.replaceState(null, '', '#journal/' + item.dataset.path);
      current = item.dataset.path;
      items.forEach(i => { i.classList.toggle('active', i === item); if (i === item) i.setAttribute('aria-current', 'page'); else i.removeAttribute('aria-current'); });
      reveal(item);
      const label = $('[data-current]'); if (label) label.textContent = current;
      keep('journal', current);
      load(item);
    },
  };
  document.addEventListener('input', e => {
    if (e.target.id === 'jnav-q') { keep('jq', e.target.value); search(e.target.value); }
  });
  document.addEventListener('click', e => {
    const t = e.target;
    if (t.closest('.jitem')) { $('.jlayout')?.classList.remove('navopen'); $('.jnav-toggle')?.setAttribute('aria-expanded', 'false'); return; }
    const toggle = t.closest('.jnav-toggle');
    if (toggle) { const open = $('.jlayout').classList.toggle('navopen'); toggle.setAttribute('aria-expanded', String(open)); return; }
    const more = t.closest('.jmore');
    if (more) {
      const group = more.closest('.jgroup');
      if (group) keep('jall:' + group.dataset.group, '1');
      (group || more.closest('.jp-section')).classList.add('all'); more.remove(); return;
    }
    const nb = t.closest('[data-jnb]');
    if (nb) { download(nb); return; }
    const filter = t.closest('[data-step-filter]');
    if (filter) { keep('jstep:' + current, filter.dataset.stepFilter); applySteps($('#jpage')); return; }
    if (t.closest('[data-step-order]')) { keep('jorder', keep('jorder', undefined, true) === 'new' ? 'old' : 'new', true); applySteps($('#jpage')); return; }
    if (t.closest('[data-step-sys]')) { keep('jsys', keep('jsys') === 'on' ? 'off' : 'on'); applySteps($('#jpage')); return; }
    const copy = t.closest('[data-copy]');
    if (copy) {
      const text = copy.dataset.copy;
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
        () => { copy.textContent = 'copied ' + text; }, () => { window.prompt('Copy this reference', text); });
    }
  });
  const q = $('#jnav-q');
  if (q && keep('jq')) { q.value = keep('jq'); search(q.value); }
  ago(document);
})();
"""
