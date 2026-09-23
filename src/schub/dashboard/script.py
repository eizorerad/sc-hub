"""Vanilla JS (~4 KB): hash routing, pipeline picker, step panel, run filter, brick
code and notebook downloads, and an auto-refresh that keeps the current view,
selection, filters and scroll position, and waits while the student reads an
opened section or types."""

SCRIPT = r"""
(() => {
  const REFRESH_MS = 60000;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const sel = (attr, v) => `[${attr}="${CSS.escape(v)}"]`;
  const keep = {
    get: (k, local) => { try { return (local ? localStorage : sessionStorage).getItem(k); } catch (e) { return null; } },
    set: (k, v, local) => { try { (local ? localStorage : sessionStorage).setItem(k, v); } catch (e) {} },
  };
  let restoring = true;
  const PALETTE = ['#4e79a7','#f28e2b','#e15759','#76b7b2','#59a14f','#edc948','#b07aa1','#ff9da7','#9c755f','#bab0ac',
    '#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf'];
  const decode = s => { try { return decodeURIComponent(s); } catch (e) { return s; } };

  // Old addresses keep working: the overview became Projects, Jobs a part of Runs.
  const ALIASES = {overview: 'projects', jobs: 'runs/queue'};
  const RUN_SECTIONS = ['history', 'queue', 'sessions'];
  const pickers = {};

  function route() {
    const firstTab = ($('[data-tab]') || {dataset: {tab: 'journal'}}).dataset.tab;
    let hash = location.hash.slice(1) || firstTab;
    const first = hash.split('/')[0];
    if (ALIASES[first]) hash = ALIASES[first];
    const [view, ...rest] = hash.split('/');
    const arg = rest.join('/');  // project paths contain '/'
    const name = $$('.view').some(v => v.dataset.view === view) ? view : firstTab;
    $$('.view').forEach(v => v.classList.toggle('active', v.dataset.view === name));
    $$('[data-tab]').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    $$('details.account .brand').forEach(b => b.classList.toggle('here', $$('[data-tab]').every(t => t.dataset.tab !== name)));
    closeMenus();
    if (name === 'runs' && RUN_SECTIONS.includes(arg)) { pickers.runs?.(arg); showRun(null); }
    else if (name === 'runs') showRun(arg ? decode(arg) : null);
    if (name === 'pipelines') selectPipe(arg || keep.get('pipe') || $('[data-pipe]')?.dataset.pipe || '');
    if (name === 'experiments' && window.SCHUB_EXPERIMENTS) window.SCHUB_EXPERIMENTS.route(decode(arg));
    if (name === 'projects') selectProject(arg ? decode(arg) : keep.get('project'));
    if (name === 'journal' && window.SCHUB_JOURNAL) window.SCHUB_JOURNAL.select(arg ? decode(arg) : null);
    if (!restoring) window.scrollTo(0, 0);
  }

  function closeMenus(except) {
    $$('details.account[open], details.menu-pop[open]').forEach(d => { if (d !== except) d.open = false; });
  }

  function selectProject(path) {
    const cards = $$('.project[data-project]');
    if (!cards.length) return;
    if (!cards.some(c => c.dataset.project === path)) path = cards[0].dataset.project;
    cards.forEach(c => { c.hidden = c.dataset.project !== path; });
    $$('.project-tree [data-project-link]').forEach(b => b.classList.toggle('active', b.dataset.projectLink === path));
    keep.set('project', path);
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* file:// pages may refuse */ }
    const area = document.createElement('textarea');
    area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    area.remove();
    return ok;
  }

  // The requests the step buttons copy (built here so the page stays small).
  function askText(kind, box) {
    const picked = $('select.ask-ref', box)?.value;
    const ref = picked || box.dataset.ref, brick = box.dataset.brick;
    const branch = picked ? picked.split('#')[0].split('/').pop() : box.dataset.branch;
    if (kind === 'ref') return ref;
    if (kind === 'jupyter') {
      const run = box.dataset.nbRun, gpu = box.dataset.nbGpu ? ' with a GPU' : '';
      return `In sc-hub, open run ${run} as a notebook: write it with make_notebook("${run}"), start a JupyterLab `
        + `session${gpu} with that notebook as the target, and tell me when I can run ./schub-lab jupyter on my laptop.`;
    }
    if (kind === 'pin') return `In sc-hub, pin branch ${ref} with label_branch (pinned=true).`;
    if (kind === 'archive') return `In sc-hub, archive branch ${ref} with label_branch (archived=true): `
      + 'it stays on disk and in the history, and leaves the Compare table unless I ask for archived ones.';
    if (kind === 'sweep') return `In sc-hub, from branch ${ref}, try step <N> with <parameter> = <values> as a sweep: `
      + 'use sweep_branch with a short name and reason, show me the plans, and submit them with submit_sweep when I confirm.';
    if (kind === 'fix') return `In sc-hub, fix step ${ref} (${brick}): <what is wrong and what it should do>. `
      + `Look at it with inspect_step("${ref}"), then use revise_branch (same branch, new revision) with a short reason, `
      + 'show me the plan, and submit it when I confirm.';
    return `In sc-hub, from step ${ref} (${brick}) on, try this instead: <the alternative>. `
      + `Look at it with inspect_step("${ref}"), then use fork_branch into a new branch (keep ${branch} as it is), `
      + 'show me the plan, and submit it when I confirm.';
  }

  // Long lists: a filter box and 'Show all N' instead of one endless table.
  function setupListings() {
    $$('.listing').forEach(box => {
      const limit = Number(box.dataset.limit) || 25, rows = $$('[data-row]', box);
      const input = $('input.list-filter', box), more = $('button.more', box), id = 'list-' + box.dataset.key;
      let expanded = keep.get(id + '-all') === '1';
      if (input) input.value = keep.get(id) || '';  // filters survive the auto-refresh
      const apply = () => {
        const q = (input?.value || '').toLowerCase();
        keep.set(id, q);
        let shown = 0, matched = 0;
        rows.forEach(r => {
          const hit = !q || r.dataset.text.includes(q);
          if (hit) matched++;
          const show = hit && (expanded || q || shown < limit);
          r.hidden = !show;
          if (show) shown++;
        });
        if (more) { more.hidden = expanded || !!q || matched <= limit; more.textContent = `Show all ${matched}`; }
      };
      if (input) input.addEventListener('input', apply);
      if (more) more.addEventListener('click', () => { expanded = true; keep.set(id + '-all', '1'); apply(); });
      apply();
    });
  }

  // Sections of one view behind pills (e.g. Library: datasets, models, references).
  function setupSubtabs() {
    $$('[data-subtabs]').forEach(group => {
      const name = group.dataset.subtabs, buttons = $$('button[data-sub]', group);
      const pick = id => {
        if (!buttons.some(b => b.dataset.sub === id)) id = buttons[0]?.dataset.sub;
        buttons.forEach(b => b.classList.toggle('active', b.dataset.sub === id));
        $$(`[data-subview="${name}"]`).forEach(v => { v.hidden = v.dataset.sub !== id; });
        keep.set('sub-' + name, id);
      };
      buttons.forEach(b => b.addEventListener('click', () => {
        pick(b.dataset.sub);
        // Runs sections have addresses (#runs/queue): keep the address in step, so a reload
        // stays here and a link to another section always changes the address.
        if (name === 'runs') history.replaceState(null, '', '#runs/' + b.dataset.sub);
      }));
      pickers[name] = pick;
      pick(keep.get('sub-' + name));
    });
  }

  function showRun(id) {
    const found = id && $(sel('data-run', id));
    $$('[data-run]').forEach(r => { r.hidden = r !== found; });
    $('#run-list').hidden = !!found;
  }

  // Brick code: copied from its template when a Code section opens, lightly colored.
  const PY = /(#[^\n]*)|("{3}[\s\S]*?"{3}|'[^'\n]*'|"[^"\n]*")|\b(def|return|if|elif|else|for|while|in|not|and|or|is|import|from|as|with|try|except|finally|raise|lambda|class|yield|pass|break|continue|None|True|False)\b|\b(\d+(?:\.\d+)?)\b/g;
  function fillCode(box) {
    const pre = $('pre.code', box), tpl = $(sel('data-code-src', box.dataset.code));
    if (!pre || pre.childNodes.length || !tpl) return;
    const text = tpl.content.textContent;
    let last = 0;
    for (const m of text.matchAll(PY)) {
      pre.append(text.slice(last, m.index));
      const span = document.createElement('span');
      span.className = m[1] ? 'c-com' : m[2] ? 'c-str' : m[3] ? 'c-key' : 'c-num';
      span.textContent = m[0];
      pre.append(span);
      last = m.index + m[0].length;
    }
    pre.append(text.slice(last));
  }

  function loadScript(src, have, done) {
    if (have()) return done(have());
    const tag = document.createElement('script');
    tag.src = src; tag.onload = () => done(have()); tag.onerror = () => done(null);
    document.head.appendChild(tag);
  }

  // A run's notebook: nb/<run>.js (scripts load on file:// pages, fetch does not), saved as .ipynb.
  function downloadNotebook(button) {
    const id = button.dataset.notebook;
    loadScript(`nb/${id}.js`, () => window.SCHUB_NB && window.SCHUB_NB[id], data => {
      if (!data) { button.textContent = 'Notebook unavailable'; return; }
      const blob = new Blob([JSON.stringify(data, null, 1)], {type: 'application/x-ipynb+json'});
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = (button.dataset.name || id) + '.ipynb';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      button.classList.add('done'); setTimeout(() => button.classList.remove('done'), 1500);
    });
  }

  function ago() {
    $$('[data-ago]').forEach(el => {
      const at = Date.parse(el.dataset.ago);
      if (!at) return;
      const minutes = Math.max(0, Math.round((Date.now() - at) / 60000));
      el.textContent = minutes < 1 ? '(just now)' : minutes < 120 ? `(${minutes} min ago)` : `(${Math.round(minutes / 60)} h ago)`;
    });
  }

  // One graph at a time: br/<view>.js (a script: file:// pages cannot fetch) registers it.
  function selectPipe(id) {
    const slot = $('#pipe-slot');
    if (!slot || !/^[a-z0-9-]+$/.test(id)) return;
    $$('[data-pipe]').forEach(b => b.classList.toggle('active', b.dataset.pipe === id));
    keep.set('pipe', id);
    const show = data => {
      if (keep.get('pipe') !== id) return;  // the student opened another graph meanwhile
      if (!data) { slot.textContent = 'This graph is not in this copy of the dashboard; it appears after the next refresh.'; clearNode(false); return; }
      slot.innerHTML = data.graph;  // written by sc-hub itself; every text in it was escaped when it was built
      const templates = $('#node-templates');
      if (templates) templates.innerHTML = data.templates;
      showHistory(keep.get('history') === '1');
      const node = keep.get('node');
      if (node) selectNode(node, false); else clearNode(false);
    };
    const have = () => window.SCHUB_BR && window.SCHUB_BR[id];
    if (have()) return show(have());
    slot.textContent = 'Loading…';
    loadScript(`br/${id}.js`, have, show);
  }

  // Older versions of steps (history) sit in a second graph per view, shown on request.
  function showHistory(on) {
    keep.set('history', on ? '1' : '0');
    $$('.graph').forEach(g => {
      const older = $('[data-history="1"]', g), now = $('[data-history="0"]', g), button = $('[data-history-toggle]', g);
      if (!older) return;
      older.hidden = !on; now.hidden = on;
      const n = Number(button.dataset.count);
      button.textContent = on ? 'Hide older steps' : `Show ${n} older step${n === 1 ? '' : 's'}`;
    });
  }

  function clearNode(forget = true) {
    const panel = $('#node-panel');
    if (panel) panel.replaceChildren(Object.assign(document.createElement('p'), {className: 'muted', textContent: 'Select a step in the graph.'}));
    $$('.pipes').forEach(p => p.classList.add('no-node'));
    $$('.lineage').forEach(svg => svg.classList.remove('focused'));
    $$('.node.selected, .node.on-path').forEach(n => n.classList.remove('selected', 'on-path'));
    if (forget) keep.set('node', '');  // a pipe without this step keeps it for when the student returns
  }

  function selectNode(key, remember = true) {
    const tpl = $(sel('data-node', key));
    const panel = $('#node-panel');
    const graph = $('[data-pipe-view]:not([hidden]) [data-history]:not([hidden])');
    const hit = graph && $('.node' + sel('data-key', key), graph);
    if (!tpl || !panel || !hit) { clearNode(remember); return; }  // not a step of the graph on screen
    const close = Object.assign(document.createElement('button'), {type: 'button', className: 'panel-close', textContent: '×'});
    close.dataset.closeNode = ''; close.setAttribute('aria-label', 'Close the step panel');
    panel.replaceChildren(close, tpl.content.cloneNode(true));
    panel.classList.remove('filled'); void panel.offsetWidth; panel.classList.add('filled');
    $$('.pipes').forEach(p => p.classList.remove('no-node'));
    $$('.node', graph).forEach(n => n.classList.remove('selected', 'on-path'));
    $('svg', graph)?.classList.add('focused');
    hit.classList.add('selected');
    (hit.dataset.path || '').split(' ').forEach(k => {
      const n = k && $('.node' + sel('data-key', k), graph);
      if (n) n.classList.add('on-path');
    });
    // The panel narrows the graph: bring a clicked step into view once the layout settled
    // (not on restore after an auto-refresh, which keeps the student's scroll position).
    if (remember) requestAnimationFrame(() => hit.scrollIntoView({block: 'nearest', inline: 'center'}));
    // The request is about the branch the student picked for this step, else the graph's branch.
    const label = graph.closest('[data-label]')?.dataset.label, pick = $('select.ask-ref', panel);
    const options = pick ? [...pick.options].map(o => o.value) : [];
    const chosen = options.find(v => v === keep.get('ask-ref-' + key)) || options.find(v => label && v.startsWith(label + '#'));
    if (chosen) pick.value = chosen;
    $$('.cellmap:not(.drawn)', panel).forEach(drawMap);
    $$('details.code[open]', panel).forEach(fillCode);
    if (remember) keep.set('node', key);
  }

  function loadPoints(src, key, done) {
    loadScript(src, () => window.SCHUB_PTS && window.SCHUB_PTS[key], done);
  }

  // A cell map: subsampled UMAP colored by one obs column; click a legend entry to focus it.
  function drawMap(box) {
    box.classList.add('drawn');
    loadPoints(box.dataset.pts, box.dataset.key, data => {
      if (!data) { box.textContent = 'Cell map unavailable.'; return; }
      const select = $('select', box), canvas = $('canvas', box), legend = $('.cm-legend', box);
      const names = Object.keys(data.cols);
      select.replaceChildren(...names.map(n => new Option(n, n)));
      select.hidden = !names.length;
      $('.cm-n', box).textContent = `${data.x.length.toLocaleString()} of ${data.n.toLocaleString()} cells`;
      let focus = -1;
      const paint = () => {
        const col = data.cols[select.value], ratio = window.devicePixelRatio || 1, size = canvas.clientWidth || 300;
        canvas.width = canvas.height = Math.round(size * ratio);
        const g = canvas.getContext('2d'), pad = 6, s = (size - 2 * pad) / 1000, r = data.x.length > 2000 ? 1.6 : 2.4;
        g.scale(ratio, ratio);
        const counts = col ? col.levels.map(() => 0) : [];
        for (let i = 0; i < data.x.length; i++) {
          const c = col ? col.codes[i] : 0;
          if (col && c >= 0) counts[c]++;
          g.globalAlpha = focus < 0 || c === focus ? 0.85 : 0.07;
          g.fillStyle = c < 0 ? '#9a9a9a' : PALETTE[c % PALETTE.length];
          g.fillRect(pad + data.x[i] * s - r / 2, pad + (1000 - data.y[i]) * s - r / 2, r, r);
        }
        legend.replaceChildren(...(col ? col.levels.map((name, k) => {
          const item = document.createElement('button'), swatch = document.createElement('i');
          item.type = 'button'; item.className = 'cm-item' + (focus === k ? ' on' : '');
          swatch.style.background = PALETTE[k % PALETTE.length];
          item.append(swatch, `${name} (${counts[k]})`);
          item.onclick = () => { focus = focus === k ? -1 : k; paint(); };
          return item;
        }) : []));
      };
      select.onchange = () => { focus = -1; paint(); };
      paint();
    });
  }

  const RUNS_SHOWN = 30;
  let runsExpanded = keep.get('runs-all') === '1';
  function filterRuns() {
    const q = ($('#run-search')?.value || '').toLowerCase();
    const st = $('#run-state')?.value || '';
    keep.set('run-q', q); keep.set('run-st', st);
    let shown = 0, matched = 0;
    $$('#run-list tbody tr').forEach(r => {
      const hit = !((st && r.dataset.state !== st) || (q && !r.dataset.text.includes(q)));
      if (hit) matched++;
      const show = hit && (runsExpanded || q || st || shown < RUNS_SHOWN);
      r.hidden = !show;
      if (show) shown++;
    });
    const more = $('#runs-more');
    if (more) more.hidden = runsExpanded || !!q || !!st || matched <= RUNS_SHOWN;
  }

  document.addEventListener('click', e => {
    // Menus: one open at a time; a click outside, or on one of its links, closes it.
    const inMenu = e.target.closest('details.account, details.menu-pop');
    closeMenus(inMenu && !e.target.closest('.menu a, .pop a') ? inMenu : null);
    const nb = e.target.closest('[data-notebook]');
    if (nb) { downloadNotebook(nb); return; }
    const ask = e.target.closest('[data-ask]');
    if (ask) {
      const box = ask.closest('.ask, .nb-box'), pick = $('select.ask-ref', box);
      if (pick && !pick.value) {  // a step several branches share: which one is the request about?
        pick.classList.add('need'); pick.focus();
        return;
      }
      const text = askText(ask.dataset.ask, box);
      copyText(text).then(ok => {
        const manual = $('textarea.manual', box), note = $('.copied', box);
        if (ok) {
          ask.classList.add('done'); setTimeout(() => ask.classList.remove('done'), 1500);
          if (note) { note.hidden = false; setTimeout(() => { note.hidden = true; }, 5000); }
          if (manual) manual.hidden = true;
        } else if (manual) {  // copying was refused: show the text to copy by hand
          manual.value = text; manual.hidden = false; manual.select();
        }
      });
      return;
    }
    if (e.target.id === 'runs-more') { runsExpanded = true; keep.set('runs-all', '1'); filterRuns(); return; }
    const project = e.target.closest('[data-project-link]');
    if (project) { location.hash = 'projects/' + project.dataset.projectLink; return; }
    if (e.target.closest('[data-history-toggle]')) {
      showHistory(keep.get('history') !== '1');
      const kept = keep.get('node');
      if (kept) selectNode(kept, false); else clearNode(false);  // keep the step if the other graph has it
      return;
    }
    const stub = e.target.closest('.node.stub[data-href]');
    if (stub) { location.hash = stub.dataset.href.replace(/^#/, ''); return; }
    const older = e.target.closest('[data-select-node]');
    if (older) { showHistory(true); selectNode(older.dataset.selectNode); return; }
    if (e.target.closest('[data-close-node]')) { clearNode(); return; }
    const node = e.target.closest('.node[data-key]');
    if (node && !node.classList.contains('ds')) return selectNode(node.dataset.key);
    const pipe = e.target.closest('[data-pipe]');
    if (pipe) { location.hash = 'pipelines/' + pipe.dataset.pipe; return; }
    // A whole row opens what it names (not when a link, button or menu inside it was used).
    const row = e.target.closest('tr[data-href], .row-link[data-href]');
    if (row && !inMenu && !e.target.closest('a, button, select, input, summary')) location.hash = row.dataset.href;
  });
  document.addEventListener('keydown', e => {
    const open = e.key === 'Escape' && $('details.account[open], details.menu-pop[open]');
    if (open) { closeMenus(); $('summary', open)?.focus(); return; }
    const node = e.key === 'Enter' && e.target.closest && e.target.closest('.node[data-key]');
    if (node && node.dataset.href) location.hash = node.dataset.href.replace(/^#/, '');
    else if (node && !node.classList.contains('ds')) selectNode(node.dataset.key);
  });
  ['input', 'change'].forEach(t => document.addEventListener(t, e => { if (e.target.closest('#run-filters')) filterRuns(); }));
  document.addEventListener('change', e => {
    if (e.target.matches('#node-panel select.ask-ref')) {
      e.target.classList.toggle('need', !e.target.value);
      keep.set('ask-ref-' + keep.get('node'), e.target.value);
    }
  });
  const filterTree = input => {
    const q = input.value.toLowerCase();
    keep.set('tree-' + (input.closest('[data-tree]')?.dataset.tree || input.placeholder), q);
    $$('button[data-text]', input.parentElement).forEach(b => { b.hidden = !!q && !b.dataset.text.includes(q); });
  };
  document.addEventListener('input', e => { if (e.target.matches('.tree-filter')) filterTree(e.target); });
  $$('.tree-filter').forEach(input => {
    input.value = keep.get('tree-' + (input.closest('[data-tree]')?.dataset.tree || input.placeholder)) || '';
    if (input.value) filterTree(input);
  });
  window.addEventListener('hashchange', route);
  document.addEventListener('toggle', e => {
    if (!e.target.open) return;
    $$('.cellmap:not(.drawn)', e.target).forEach(drawMap);
    if (e.target.matches('details.code')) fillCode(e.target);
  }, true);

  const auto = () => keep.get('autorefresh', true) !== 'off';
  const button = $('#autorefresh');
  const paint = () => {
    button.setAttribute('aria-checked', String(auto()));
    $$('.paused').forEach(p => { p.hidden = auto(); });
  };
  button.addEventListener('click', () => { keep.set('autorefresh', auto() ? 'off' : 'on', true); paint(); });
  // Not while the student types or reads an opened section (a reload would close it).
  // Filters survive reloads, so only recent typing counts, not a lingering focus.
  let typedAt = 0;
  document.addEventListener('input', () => { typedAt = Date.now(); });
  const busy = () => Date.now() - typedAt < 15000 ||
    $$('.view.active details[open], #node-panel details[open], details.account[open], textarea.manual:not([hidden])')
      .some(d => d.getClientRects().length);
  setInterval(() => {
    if (auto() && !document.hidden && !busy()) { keep.set('scroll', String(window.scrollY)); location.reload(); }
  }, REFRESH_MS);

  if ($('#run-search')) { $('#run-search').value = keep.get('run-q') || ''; }
  const st = $('#run-state');
  if (st && [...st.options].some(o => o.value === keep.get('run-st'))) st.value = keep.get('run-st');
  setupListings(); setupSubtabs();
  filterRuns();
  route(); paint(); ago();
  setInterval(ago, 30000);
  const y = Number(keep.get('scroll') || 0);
  if (y) window.scrollTo(0, y);
  keep.set('scroll', '0');
  restoring = false;
})();
"""
